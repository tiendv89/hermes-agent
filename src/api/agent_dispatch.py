"""Shared agent-dispatch state and worker-thread turn executor.

Centralises:
  - _active_runs / _active_runs_lock  (in-flight session guard, shared with legacy /chat)
  - _pending_agent_turns / _pending_lock  (coalescing pending follow-up turns)
  - _run_agent_turn()  (blocking worker-thread function)
  - schedule_agent_turn()  (async entry point for the send service)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
from typing import Any, Callable, Dict, Optional, Set

import re as _re

from src.realtime.bus import get_bus
from src.streaming import BusPublishingSSETranslator, HermesSSETranslator

logger = logging.getLogger(__name__)

_WORD_RE = _re.compile(r"\S+\s*|\s+")


def _make_delta_callback(cb: Callable) -> Callable:
    """Wrap a stream-delta callback to split large chunks into word-sized pieces.

    Controlled by HERMES_STREAM_CHUNK_CHARS (default 0 = word-split enabled).
    Set to a positive integer to use fixed-size chunks, or -1 to disable
    splitting entirely (raw model chunks, may be 50-100 chars each).

    Example .env settings:
        HERMES_STREAM_CHUNK_CHARS=0   # word-by-word split (default)
        HERMES_STREAM_CHUNK_CHARS=4   # fixed 4-char chunks
        HERMES_STREAM_CHUNK_CHARS=-1  # no splitting
    """
    chunk_chars = int(os.environ.get("HERMES_STREAM_CHUNK_CHARS", "0"))

    if chunk_chars < 0:
        # Splitting disabled — pass through raw.
        return cb

    if chunk_chars == 0:
        # Word-by-word split.
        def _word_cb(delta: Any = None, **kwargs: Any) -> None:
            if not delta:
                return
            for part in (_WORD_RE.findall(str(delta)) or [str(delta)]):
                cb(part, **kwargs)
        return _word_cb

    # Fixed-size chunks.
    def _fixed_cb(delta: Any = None, **kwargs: Any) -> None:
        if not delta:
            return
        text = str(delta)
        for i in range(0, len(text), chunk_chars):
            cb(text[i:i + chunk_chars], **kwargs)
    return _fixed_cb

# ---------------------------------------------------------------------------
# Shared in-flight guard (also used by the legacy /chat route)
# ---------------------------------------------------------------------------

_active_runs: Set[str] = set()
_active_runs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Coalescing: pending follow-up turns
#
# When @agent arrives while a turn is in flight, we record the request here
# instead of starting a second concurrent turn.  When the current turn ends
# its finally-block checks this dict and schedules one follow-up turn.
# ---------------------------------------------------------------------------

_pending_agent_turns: Dict[str, Dict[str, Any]] = {}
_pending_lock = threading.Lock()


async def _backfill_assistant(db_factory: Callable, session_id: str, content: str) -> None:
    """Safety-net persist of the assistant reply.

    The agent's own GatewaySessionDB mirror normally persists the turn's
    assistant message(s) per model iteration (text/tool_calls/tool rows) —
    that full-fidelity record is what the model's follow-up context needs, and
    the UI coalesces it into one bubble on reload.

    This backfill only writes when the mirror persisted NO assistant row for
    the turn at all (the conversation-compression edge case where the agent's
    session_id rotated mid-turn). Writing unconditionally would append a
    concatenated duplicate on top of the per-iteration rows, so the live stream
    (one coalesced bubble) and the reloaded transcript would diverge.
    """
    from src.db import get_session_messages
    from src.db.store import append_message

    async with db_factory() as db:
        existing = await get_session_messages(db, session_id)
        # Find the last user message; an assistant row after it means the
        # mirror already captured this turn — nothing to backfill.
        last_user_idx = -1
        for i, m in enumerate(existing):
            if m.get("role") == "user":
                last_user_idx = i
        has_assistant_this_turn = any(
            m.get("role") == "assistant" for m in existing[last_user_idx + 1 :]
        )
        if not has_assistant_this_turn:
            await append_message(db, session_id, role="assistant", content=content)


# ---------------------------------------------------------------------------
# Worker-thread agent executor
# ---------------------------------------------------------------------------


def _run_agent_turn(
    *,
    session_id: str,
    message: str,
    history: list,
    workspace_id: str,
    feature_id: str,
    user_id: str,
    model: str,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    db_factory: Callable,
    loop: asyncio.AbstractEventLoop,
    translator: HermesSSETranslator,
    author_id: Optional[str] = None,
    skip_user_persist: bool = False,
) -> None:
    """Run one blocking agent turn on a worker thread, streaming via *translator*.

    Handles the full run lifecycle including coalescing pending follow-up turns.
    """
    workflow_context = None
    try:
        from plugins import context as workflow_context

        workflow_context.set_context(session_id, workspace_id, feature_id)
        workflow_context.set_agent_context(session_id, loop, db_factory)

        # Input scope guard — enforce shared.md's "stay on-topic" rule before
        # the agent runs. Confidently off-topic messages get the canned decline
        # without invoking the agent at all. Fails open (see scope_guard).
        from src.api.scope_guard import SCOPE_DECLINE, is_out_of_scope

        if is_out_of_scope(
            message, provider=provider, model=model, api_key=api_key, base_url=base_url
        ):
            logger.info(
                "agent_dispatch: declining out-of-scope message for session %s",
                session_id,
            )
            _make_delta_callback(translator.on_delta)(SCOPE_DECLINE)

            async def _persist_decline() -> None:
                from src.db.store import append_message

                async with db_factory() as db:
                    if not skip_user_persist:
                        await append_message(
                            db, session_id, role="user", content=message, author_id=author_id
                        )
                    await append_message(
                        db, session_id, role="assistant", content=SCOPE_DECLINE
                    )

            try:
                asyncio.run_coroutine_threadsafe(_persist_decline(), loop).result(timeout=15)
            except Exception:
                logger.exception(
                    "agent_dispatch: scope-decline persist failed for session %s", session_id
                )
            return  # finally-block runs: done(), clear context, release run, coalesce

        try:
            from src.db.session_db_proxy import make_gateway_session_db

            session_db = make_gateway_session_db(
                loop,
                db_factory,
                session_id,
                author_id=author_id,
                skip_user_persist=skip_user_persist,
            )
        except Exception:
            logger.exception(
                "agent_dispatch: gateway session DB unavailable for %s; transcript not mirrored",
                session_id,
            )
            session_db = None

        from plugins.skills import get_shared_rules

        shared_rules = get_shared_rules() or None

        from run_agent import AIAgent

        agent = AIAgent(
            model=model,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            enabled_toolsets=["workflow"],
            max_iterations=int(os.environ.get("HERMES_MAX_ITERATIONS", "90")),
            quiet_mode=True,
            platform="workflow_gateway",
            ephemeral_system_prompt=shared_rules,
            session_id=session_id,
            user_id=user_id or None,
            gateway_session_key=session_id,
            session_db=session_db,
            stream_delta_callback=_make_delta_callback(translator.on_delta),
            tool_start_callback=translator.on_tool_start,
            tool_complete_callback=translator.on_tool_complete,
        )
        agent.run_conversation(message, conversation_history=history)

        # Backfill: ensure the assistant reply is persisted under THIS session.
        # The agent's own mirror can miss it (e.g. conversation compression
        # rotates agent.session_id, so the proxy stops mirroring to the channel
        # id) — which showed up as the reply vanishing on reload. Append the
        # accumulated reply only if it isn't already the last stored message
        # (dedupe so we never double-write when the agent did persist it).
        final_text = (getattr(translator, "full_text", "") or "").strip()
        if final_text:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    _backfill_assistant(db_factory, session_id, final_text), loop
                )
                fut.result(timeout=15)
            except Exception:
                logger.exception(
                    "agent_dispatch: assistant backfill failed for session %s", session_id
                )
    except Exception as exc:
        logger.exception("agent_dispatch: agent turn failed for session %s", session_id)
        translator.on_error(str(exc))
    finally:
        translator.done()
        if workflow_context is not None:
            workflow_context.clear_context(session_id)
        with _active_runs_lock:
            _active_runs.discard(session_id)

        # Coalescing: if a follow-up @agent mention arrived while we were running,
        # schedule exactly one new turn now (not N).
        with _pending_lock:
            pending = _pending_agent_turns.pop(session_id, None)

        if pending:
            asyncio.run_coroutine_threadsafe(
                _schedule_follow_up(session_id, pending, loop),
                loop,
            )


async def _schedule_follow_up(
    session_id: str,
    pending: Dict[str, Any],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Re-load history and schedule the coalesced follow-up turn."""
    try:
        from src.db import get_messages_as_conversation, touch_session
        from src.api.model_catalog import resolve_model

        async with pending["db_factory"]() as db:
            history = await get_messages_as_conversation(db, session_id)
            await touch_session(db, session_id)

        resolved = resolve_model(pending["model"])

        with _active_runs_lock:
            if session_id in _active_runs:
                # Shouldn't happen, but guard against races.
                return
            _active_runs.add(session_id)

        follow_translator = BusPublishingSSETranslator(
            session_id=session_id, model=resolved["model"]
        )
        get_bus().publish(
            session_id, {"event": "agent.working", "data": {"session_id": session_id}}
        )
        loop.run_in_executor(
            None,
            functools.partial(
                _run_agent_turn,
                session_id=session_id,
                message=pending["message"],
                history=history,
                workspace_id=pending["workspace_id"],
                feature_id=pending["feature_id"],
                user_id=pending["user_id"],
                model=resolved["model"],
                provider=resolved["provider"],
                api_key=resolved["api_key"],
                base_url=resolved["base_url"],
                db_factory=pending["db_factory"],
                loop=loop,
                translator=follow_translator,
                skip_user_persist=True,
            ),
        )
    except Exception:
        logger.exception(
            "agent_dispatch: failed to schedule follow-up turn for %s", session_id
        )
        with _active_runs_lock:
            _active_runs.discard(session_id)


async def schedule_agent_turn(
    *,
    session_id: str,
    message: str,
    history: list,
    workspace_id: str,
    feature_id: str,
    user_id: str,
    model: str,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    db_factory: Callable,
    loop: asyncio.AbstractEventLoop,
    author_id: Optional[str] = None,
    skip_user_persist: bool = False,
) -> bool:
    """Schedule an agent turn with coalescing.

    If a turn is already in flight for session_id, record a pending follow-up
    (coalescing: only one follow-up regardless of how many @agent mentions
    arrived during the in-flight turn) and return False.

    Otherwise, claim the session and schedule the turn on the thread pool;
    return True.
    """
    with _active_runs_lock:
        if session_id in _active_runs:
            # Turn already in flight — record a pending follow-up (coalesce).
            with _pending_lock:
                _pending_agent_turns[session_id] = {
                    "message": message,
                    "workspace_id": workspace_id,
                    "feature_id": feature_id,
                    "user_id": user_id,
                    "model": model,
                    "db_factory": db_factory,
                }
            logger.debug(
                "agent_dispatch: coalesced pending turn for %s (turn already in flight)",
                session_id,
            )
            return False
        _active_runs.add(session_id)

    translator = BusPublishingSSETranslator(session_id=session_id, model=model)

    # Signal to all stream subscribers that the agent is starting work.
    get_bus().publish(
        session_id, {"event": "agent.working", "data": {"session_id": session_id}}
    )

    loop.run_in_executor(
        None,
        functools.partial(
            _run_agent_turn,
            session_id=session_id,
            message=message,
            history=history,
            workspace_id=workspace_id,
            feature_id=feature_id,
            user_id=user_id,
            model=model,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            db_factory=db_factory,
            loop=loop,
            translator=translator,
            author_id=author_id,
            skip_user_persist=skip_user_persist,
        ),
    )
    return True
