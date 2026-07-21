"""workflow plugin — registers workspace-aware tools for the digital-factory agent.

This module provides the shared tool-registration infrastructure used by
all profiles. Each profile calls ``register(ctx, tools=...)`` with its own
tool list. The module-level ``_TOOLS`` tuple is kept as an empty backward-
compatibility fallback; new code should always pass ``tools`` explicitly.

Shared utilities:
- ``_guardrail_wrapper`` — wraps a handler with pre-dispatch guardrail checks
- ``_json_result_handler`` — wraps a handler for JSON-stringify + sanitization
- ``_as_tool_content`` — coerces a handler return value to a string
- ``_unpack_args`` — merges positional + keyword args into a single kwargs dict
- ``_get_session_context`` — returns the current session's workspace context
"""

from __future__ import annotations

import functools
import json
import logging
from typing import Any

from .tools import guardrails as _guardrails

logger = logging.getLogger(__name__)


def _as_tool_content(result: Any) -> str:
    """Coerce a handler's return value to a string for the tool message content.

    The agent's tool registry passes a handler's return value straight through
    as the ``tool`` message ``content`` (it only JSON-encodes errors). Our
    handlers return dicts (``{\"ok\": True, ...}``). The Anthropic adapter
    stringifies dict content, but strict OpenAI-compatible providers (DeepSeek)
    reject it with HTTP 400 "content should be a string or a list". JSON-encode
    here so the wire content is always a string, for every provider.
    """
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(result)


def _unpack_args(args: tuple, kwargs: dict) -> dict:
    """Merge positional and keyword args into a single kwargs dict.

    registry.dispatch calls entry.handler(args_dict, **extra_kwargs) where the
    first positional argument is the full tool-call arguments dict from the
    model. Handlers are defined with named parameters (``stage``, ``content``,
    etc.), so the dict must be unpacked — not passed as a positional — or every
    named parameter receives the entire dict as its value.

    Extra registry kwargs (task_id, session_id, user_task) are merged in after
    the tool args so handlers can absorb them with ``**_``.
    """
    fn_args = args[0] if args and isinstance(args[0], dict) else {}
    return {**fn_args, **kwargs}


def _get_session_context() -> dict[str, Any] | None:
    """Return the current session's workspace context for guardrail G10.

    Uses the agent_session_id stored on the thread-local by set_agent_context()
    at turn start to look up the authoritative per-session store. Returns None
    when no agent session is registered (general chat without workspace binding),
    which causes G10 to be skipped.

    Reads from the per-session dict (not the raw thread-local workspace_id /
    feature_id) to avoid the G2 leakage: a reused ThreadPoolExecutor thread
    retains stale thread-locals from the previous session; agent_session_id is
    always overwritten at the start of each turn by set_agent_context().
    """
    try:
        from plugins.context import get_agent_session_id, get_context_for_session

        session_id = get_agent_session_id()
        if session_id:
            workspace_id, feature_id = get_context_for_session(session_id)
            if workspace_id:
                return {"workspace_id": workspace_id, "feature_id": feature_id}
    except Exception:
        pass
    return None


def _guardrail_wrapper(handler: Any, tool_name: str, is_async: bool) -> Any:
    """Wrap a tool handler with the pre-dispatch guardrail gate (T2).

    Checks guardrails.check() before invoking the handler. If a guardrail
    blocks the call, returns a structured refusal JSON string without calling
    the handler. Fail-closed: if the guardrails module is unavailable, the
    call is allowed to proceed (fail-open only on import error, never on a
    normal guardrail exception — guardrails.check() itself is fail-closed).
    """
    try:
        from plugins.tools import guardrails as _guardrails_mod
    except ImportError:
        logger.error(
            "guardrail_wrapper: could not import guardrails module — tool %s will run unguarded",
            tool_name,
        )
        return handler

    if is_async:

        @functools.wraps(handler)
        async def _async_guarded(*args: Any, **kwargs: Any) -> str:
            arguments = args[0] if args and isinstance(args[0], dict) else {}
            session_context = _get_session_context()
            allowed, reason_code = _guardrails_mod.check(
                tool_name, arguments, session_context=session_context
            )
            if not allowed:
                refusal = _guardrails_mod.build_refusal_message(reason_code, tool_name)
                logger.info(
                    "guardrail pre-dispatch blocked: tool=%s reason=%s",
                    tool_name,
                    reason_code,
                )
                return _as_tool_content(refusal)
            return await handler(*args, **kwargs)

        return _async_guarded

    @functools.wraps(handler)
    def _sync_guarded(*args: Any, **kwargs: Any) -> str:
        arguments = args[0] if args and isinstance(args[0], dict) else {}
        session_context = _get_session_context()
        allowed, reason_code = _guardrails_mod.check(
            tool_name, arguments, session_context=session_context
        )
        if not allowed:
            refusal = _guardrails_mod.build_refusal_message(reason_code, tool_name)
            logger.info(
                "guardrail pre-dispatch blocked: tool=%s reason=%s",
                tool_name,
                reason_code,
            )
            return _as_tool_content(refusal)
        return handler(*args, **kwargs)

    return _sync_guarded


def _json_result_handler(handler: Any, is_async: bool, tool_name: str = "") -> Any:
    """Wrap a tool handler so its return value is sanitized and JSON-stringified.

    Unpacks the positional args-dict from registry.dispatch into keyword
    arguments before calling the handler, then applies guardrail result
    sanitization (G7 OOB marker stripping) and JSON-encodes the return value.
    The underlying ``handle()`` still returns its dict to direct callers
    (hooks, HTTP routes, unit tests) — only the registered tool handler is
    wrapped.
    """
    if is_async:

        @functools.wraps(handler)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> str:
            result = await handler(**_unpack_args(args, kwargs))
            result = _guardrails.sanitize_result(tool_name, result)
            return _as_tool_content(result)

        return _async_wrapper

    @functools.wraps(handler)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> str:
        result = handler(**_unpack_args(args, kwargs))
        result = _guardrails.sanitize_result(tool_name, result)
        return _as_tool_content(result)

    return _sync_wrapper


# ---------------------------------------------------------------------------
# Legacy module-level tool list — kept empty for backward compatibility.
# Profiles must pass their own tool list to register().
# ---------------------------------------------------------------------------

_TOOLS: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# Tool registration entry point
# ---------------------------------------------------------------------------


def register(ctx: Any, tools: tuple[dict[str, Any], ...] | None = None) -> None:
    """Register a set of tools on the agent context.

    Each tool dict must have: ``name``, ``schema``, ``handler``.
    Optional keys: ``short_description``, ``check_fn``, ``is_async``.

    Args:
        ctx: Agent PluginContext with a ``register_tool`` method.
        tools: Tuple of tool dicts to register. If ``None``, falls back to
               the module-level ``_TOOLS`` (for backward compatibility).
    """
    global _TOOLS

    if tools is not None:
        _tools = tools
        _TOOLS = tools
    else:
        _tools = _TOOLS
        # If _TOOLS is empty and no explicit tools were passed, try
        # loading the default workflow tools. This preserves backward
        # compatibility for callers that do plugins.register(ctx) with
        # no tools argument after the T2 profile split.
        if not _tools:
            try:
                from profiles.workflow.setup import _WORKFLOW_TOOLS

                _tools = _WORKFLOW_TOOLS
                _TOOLS = _WORKFLOW_TOOLS
            except ImportError:
                pass

    for t in _tools:
        is_async = t.get("is_async", False)
        # Apply JSON-result wrapping first, then the pre-dispatch guardrail gate.
        # Order: AIAgent → _guardrail_wrapper → _json_result_handler → handler
        json_handler = _json_result_handler(t["handler"], is_async, t["name"])
        guarded_handler = _guardrail_wrapper(json_handler, t["name"], is_async)
        ctx.register_tool(
            name=t["name"],
            toolset="workflow",
            schema=t["schema"],
            handler=guarded_handler,
            check_fn=t.get("check_fn"),
            is_async=is_async,
        )
        logger.debug("plugins: registered tool %s", t["name"])

    logger.info("plugins: registered %d tools", len(_tools))
