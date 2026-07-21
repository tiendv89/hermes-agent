"""Streaming chat route for the IDE coding-agent profile.

POST /coding/chat — run one agent turn with coding tools and stream the
reply back as SSE, including ``hermes.tool.deferred`` events for tools
the IDE extension must execute client-side.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import uuid
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from src.api.coding_identity import CodingIdentity, require_coding_identity
from src.services.cost_client import QuotaCheckResult, check_quota, emit_turn_cost
from src.streaming import HermesSSETranslator

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class IDEFileContext(BaseModel):
    """A single file's state as reported by the IDE extension."""

    path: str = ""
    language: str = ""
    cursor_line: int = 0
    selection: Optional[str] = None


class IDEContext(BaseModel):
    """Context gathered by the IDE extension on every message."""

    active_file: str = ""
    active_file_language: str = ""
    cursor_line: int = 0
    selection: Optional[str] = None
    open_files: List[IDEFileContext] = []
    git_branch: str = ""
    git_status: str = ""
    diagnostics: str = ""
    workspace_root: str = ""


class CodingChatRequest(BaseModel):
    """Request body for POST /coding/chat."""

    messages: List[dict]
    workspace_id: str
    feature_id: Optional[str] = None
    repo_path: str = ""
    context: IDEContext = IDEContext()
    model: str = ""


# ---------------------------------------------------------------------------
# IDE context → system prompt
# ---------------------------------------------------------------------------


def _build_system_context(ctx: IDEContext) -> str:
    """Render the IDE context block that is injected into the system prompt.

    The agent sees this as an additional system message so it knows the
    developer's editor state without wasting a turn asking for it.
    """
    lines: List[str] = [
        "## IDE context (from the developer's editor)",
        f"- Workspace root: {ctx.workspace_root or '(unknown)'}",
        f"- Active file: {ctx.active_file or '(none)'}",
        f"- Cursor: line {ctx.cursor_line}",
    ]

    if ctx.selection:
        sel = ctx.selection
        if len(sel) > 2000:
            sel = sel[:2000] + "\n... (selection truncated)"
        lines.append(f"- Selection:\n```\n{sel}\n```")

    if ctx.open_files:
        open_list = ", ".join(f.path for f in ctx.open_files[:10])
        lines.append(f"- Open files: {open_list}")

    if ctx.diagnostics:
        diag = ctx.diagnostics
        if len(diag) > 2000:
            diag = diag[:2000] + "\n... (truncated)"
        lines.append(f"- Diagnostics:\n```\n{diag}\n```")

    if ctx.git_branch:
        lines.append(f"- Git branch: {ctx.git_branch}")

    if ctx.git_status:
        gs = ctx.git_status
        if len(gs) > 1000:
            gs = gs[:1000] + "\n... (truncated)"
        lines.append(f"- Git status:\n```\n{gs}\n```")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Agent turn — blocking worker-thread function
# ---------------------------------------------------------------------------


def _run_coding_agent_turn(
    *,
    run_id: str,
    messages: List[dict],
    system_context: str,
    workspace_id: str,
    feature_id: str,
    user_id: str,
    org_id: str,
    model: str,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    loop: asyncio.AbstractEventLoop,
    translator: HermesSSETranslator,
) -> None:
    """Run one blocking coding-agent turn on a worker thread.

    Mirrors :func:`src.api.agent_dispatch._run_agent_turn` but is simpler:
    the coding agent has no session DB, scope guard, image handling, or
    clarify callback — the IDE is the hands, the agent is the brain.
    """
    try:
        # ── Pre-turn quota guard ──────────────────────────────────────
        try:
            quota: QuotaCheckResult = asyncio.run_coroutine_threadsafe(
                check_quota("coding-" + run_id, user_id, org_id=org_id),
                loop,
            ).result(timeout=5)
        except Exception:
            logger.exception(
                "coding_chat: quota check timed out for run %s (fail-open)", run_id
            )
            quota = QuotaCheckResult.fail_open()

        if not quota.allowed:
            reason = quota.reason or "quota_exceeded"
            resets_at = quota.resets_at or ""
            cap_label = (
                f"daily credit limit ({quota.daily_cap})"
                if quota.daily_cap
                else "daily credit limit"
            ) if reason == "daily_exceeded" else (
                f"weekly credit limit ({quota.weekly_cap})"
                if quota.weekly_cap
                else "weekly credit limit"
            ) if reason == "weekly_exceeded" else "credit limit"
            block_msg = (
                f"You've reached your {cap_label}. "
                f"Your quota resets at {resets_at}."
                if resets_at
                else f"You've reached your {cap_label}."
            )
            logger.info(
                "coding_chat: quota blocked turn for run %s reason=%s", run_id, reason
            )
            translator.on_delta(block_msg)
            return

        # ── Build the agent ──────────────────────────────────────────
        from plugins.skills import get_shared_rules

        shared_rules = get_shared_rules() or None

        _reasoning_effort = (
            os.environ.get("HERMES_REASONING_EFFORT", "medium").strip().lower()
        )
        _reasoning_off = _reasoning_effort in ("", "off", "none", "disabled")
        reasoning_config = (
            None if _reasoning_off else {"enabled": True, "effort": _reasoning_effort}
        )

        from run_agent import AIAgent

        agent = AIAgent(
            model=model,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            enabled_toolsets=["coding"],
            max_iterations=int(os.environ.get("HERMES_MAX_ITERATIONS", "90")),
            quiet_mode=True,
            platform="coding_agent",
            ephemeral_system_prompt=(
                system_context
                if not shared_rules
                else f"{shared_rules}\n\n{system_context}"
            ),
            session_id=f"coding_{run_id}",
            user_id=user_id or None,
            stream_delta_callback=_make_delta_cb(translator.on_delta),
            tool_start_callback=translator.on_tool_start,
            tool_complete_callback=translator.on_tool_complete,
            reasoning_callback=_make_delta_cb(translator.on_reasoning),
            reasoning_config=reasoning_config,
        )

        # ── Run the conversation ─────────────────────────────────────
        conversation_history = messages[:-1] if len(messages) > 1 else []
        last_message = messages[-1]["content"] if messages else ""

        agent.run_conversation(
            last_message,
            conversation_history=conversation_history,
        )

        # ── Post-turn cost emission ──────────────────────────────────
        try:
            asyncio.run_coroutine_threadsafe(
                emit_turn_cost(
                    "coding-" + run_id,
                    user_id,
                    model,
                    input_tokens=getattr(agent, "session_input_tokens", 0),
                    output_tokens=getattr(agent, "session_output_tokens", 0),
                    cache_read_tokens=getattr(agent, "session_cache_read_tokens", 0),
                    cache_write_tokens=getattr(agent, "session_cache_write_tokens", 0),
                    stopped=False,
                    turn_id=run_id,
                    org_id=org_id,
                    source_label=feature_id or workspace_id or run_id,
                ),
                loop,
            ).result(timeout=15)
        except Exception:
            logger.exception(
                "coding_chat: post-turn cost emission failed for run %s", run_id
            )

    except Exception as exc:
        logger.exception("coding_chat: agent turn failed for run %s", run_id)
        translator.on_error(str(exc))
    finally:
        translator.done()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Chunk-splitting — same word-by-word strategy as agent_dispatch.py.
_WORD_RE = __import__("re").compile(r"\S+\s*|\s+")


def _make_delta_cb(cb: Any) -> Any:
    """Wrap a stream-delta callback to split large chunks word-by-word."""
    chunk_chars = int(os.environ.get("HERMES_STREAM_CHUNK_CHARS", "0"))

    if chunk_chars < 0:
        return cb

    if chunk_chars == 0:

        def _word_cb(delta: Any = None, **kwargs: Any) -> None:
            if not delta:
                return
            for part in _WORD_RE.findall(str(delta)) or [str(delta)]:
                cb(part, **kwargs)

        return _word_cb

    def _fixed_cb(delta: Any = None, **kwargs: Any) -> None:
        if not delta:
            return
        text = str(delta)
        for i in range(0, len(text), chunk_chars):
            cb(text[i : i + chunk_chars], **kwargs)

    return _fixed_cb


def _resolve_model() -> dict:
    """Resolve the coding agent model from environment or fallback."""
    return {
        "model": os.environ.get("CODING_AGENT_MODEL", "claude-sonnet-4-6"),
        "provider": os.environ.get("CODING_AGENT_PROVIDER") or None,
        "api_key": os.environ.get("CODING_AGENT_API_KEY") or None,
        "base_url": os.environ.get("CODING_AGENT_BASE_URL") or None,
    }


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/coding/chat")
async def coding_chat(
    body: CodingChatRequest,
    request: Request,
    identity: CodingIdentity = Depends(require_coding_identity),
) -> StreamingResponse:
    """Run one agent turn with coding tools and stream the reply via SSE.

    The IDE extension gathers context (active file, selection, git status,
    diagnostics) and sends it in the request body.  The agent injects it
    into its system prompt, runs the turn, and streams back:

    * ``chat.completion.chunk`` — text deltas (OpenAI-compatible).
    * ``hermes.tool.deferred`` — a tool the IDE must execute locally.
    * ``hermes.tool.progress`` — server-side tool start/complete.
    * ``agent.reasoning`` — reasoning-trace (when enabled).
    * ``[DONE]`` — end-of-stream sentinel.

    The extension sends tool results back as ``role: "tool"`` messages
    in the ``messages`` array of the next request.
    """
    run_id = uuid.uuid4().hex
    caller_id = identity.user_id

    resolved = _resolve_model()

    system_context = _build_system_context(body.context)

    translator = HermesSSETranslator(model=resolved["model"])
    loop = asyncio.get_running_loop()

    asyncio.ensure_future(
        loop.run_in_executor(
            None,
            functools.partial(
                _run_coding_agent_turn,
                run_id=run_id,
                messages=body.messages,
                system_context=system_context,
                workspace_id=body.workspace_id,
                feature_id=body.feature_id,
                user_id=caller_id,
                org_id=identity.org_id,
                model=resolved["model"],
                provider=resolved["provider"],
                api_key=resolved["api_key"],
                base_url=resolved["base_url"],
                loop=loop,
                translator=translator,
            ),
        )
    )

    return StreamingResponse(
        translator.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
