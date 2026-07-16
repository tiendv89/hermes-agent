"""workflow plugin — registers workspace-aware tools for the digital-factory agent."""

from __future__ import annotations

import functools
import json
import logging
from typing import Any, Optional

from src.services.workflow_backend_client import check_workflow_available
from .hooks import inject_context
from .tools import guardrails as _guardrails
from .tools import (
    workspace,
    feature,
    artifacts,
    edit as edit_tool,
    file_ops as file_ops_tool,
    read as read_tool,
    read_workspace_file as read_workspace_file_tool,
    list_documents as list_documents_tool,
    tasks as tasks_tool,
    gitnexus,
    rag,
    skills as skills_tool,
    approval,
    approve as approve_tool,
    tasks_write as tasks_write_tool,
    suggest_next_actions as suggest_next_actions_tool,
    create_tasks as create_tasks_tool,
    parse_tasks as parse_tasks_tool,
    github_pr_context as github_pr_context_tool,
    github_pr_review as github_pr_review_tool,
    lookup_feature as lookup_feature_tool,
    init_feature as init_feature_tool,
)

logger = logging.getLogger(__name__)


def _as_tool_content(result: Any) -> str:
    """Coerce a handler's return value to a string for the tool message content.

    The agent's tool registry passes a handler's return value straight through
    as the ``tool`` message ``content`` (it only JSON-encodes errors). Our
    handlers return dicts (``{"ok": True, ...}``). The Anthropic adapter
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


def _get_session_context() -> Optional[dict[str, Any]]:
    """Return the current session's workspace context for guardrail G10.

    Uses the thread-local set by set_context() at turn start. Returns None
    when no context is set (general chat without workspace binding), which
    causes the workspace isolation check to be skipped.
    """
    try:
        from plugins.context import get_workspace_id, get_feature_id

        workspace_id = get_workspace_id()
        if workspace_id:
            return {"workspace_id": workspace_id, "feature_id": get_feature_id()}
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


_TOOLS = (
    {
        "name": "get_workspace_context",
        "schema": workspace.SCHEMA,
        "handler": workspace.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "get_feature_state",
        "schema": feature.SCHEMA,
        "handler": feature.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "write_file",
        "schema": file_ops_tool.WRITE_FILE_SCHEMA,
        "handler": file_ops_tool.handle_write_file,
        "check_fn": check_workflow_available,
    },
    {
        "name": "edit_file",
        "schema": file_ops_tool.EDIT_FILE_SCHEMA,
        "handler": file_ops_tool.handle_edit_file,
        "check_fn": check_workflow_available,
    },
    {
        "name": "write_product_spec",
        "schema": artifacts.WRITE_SPEC_SCHEMA,
        "handler": artifacts.handle_write_product_spec,
        "check_fn": check_workflow_available,
    },
    {
        "name": "read_file",
        "schema": read_tool.READ_FILE_SCHEMA,
        "handler": read_tool.handle_read_file,
        "check_fn": check_workflow_available,
    },
    {
        "name": "edit_document",
        "schema": edit_tool.EDIT_DOCUMENT_SCHEMA,
        "handler": edit_tool.handle_edit_document,
        "check_fn": check_workflow_available,
    },
    {
        "name": "read_workspace_file",
        "schema": read_workspace_file_tool.SCHEMA,
        "handler": read_workspace_file_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "list_documents",
        "schema": list_documents_tool.SCHEMA,
        "handler": list_documents_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "write_technical_design",
        "schema": artifacts.WRITE_TD_SCHEMA,
        "handler": artifacts.handle_write_technical_design,
        "check_fn": check_workflow_available,
    },
    {
        "name": "get_tasks",
        "schema": tasks_tool.SCHEMA,
        "handler": tasks_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "query_gitnexus",
        "schema": gitnexus.SCHEMA,
        "handler": gitnexus.handle,
        "check_fn": gitnexus.check_available,
        "is_async": True,
    },
    {
        "name": "query_rag",
        "schema": rag.SCHEMA,
        "handler": rag.handle,
        "check_fn": rag.check_available,
        "is_async": True,
    },
    {
        "name": "load_skill",
        "schema": skills_tool.SCHEMA,
        "handler": skills_tool.handle,
        "check_fn": skills_tool.check_available,
    },
    {
        "name": "request_approval",
        "schema": approval.SCHEMA,
        "handler": approval.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "approve_feature",
        "schema": approve_tool.SCHEMA,
        "handler": approve_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "write_tasks",
        "schema": tasks_write_tool.SCHEMA,
        "handler": tasks_write_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "create_tasks",
        "schema": create_tasks_tool.SCHEMA,
        "handler": create_tasks_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "parse_tasks",
        "schema": parse_tasks_tool.SCHEMA,
        "handler": parse_tasks_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "suggest_next_actions",
        "schema": suggest_next_actions_tool.SCHEMA,
        "handler": suggest_next_actions_tool.handle,
    },
    {
        "name": "github_pr_context",
        "schema": github_pr_context_tool.SCHEMA,
        "handler": github_pr_context_tool.handle,
        "check_fn": github_pr_context_tool.check_available,
    },
    {
        "name": "github_pr_review",
        "schema": github_pr_review_tool.SCHEMA,
        "handler": github_pr_review_tool.handle,
        "check_fn": github_pr_review_tool.check_available,
    },
    {
        "name": "workflow_lookup_feature",
        "schema": lookup_feature_tool.SCHEMA,
        "handler": lookup_feature_tool.handle,
        "check_fn": lookup_feature_tool.check_available,
    },
    {
        "name": "workflow_init_feature",
        "schema": init_feature_tool.SCHEMA,
        "handler": init_feature_tool.handle,
        "check_fn": check_workflow_available,
    },
)


def register(ctx: Any) -> None:
    """Entry point called by PluginManager.discover_and_load."""
    for t in _TOOLS:
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
        logger.debug("workflow plugin: registered tool %s", t["name"])

    ctx.register_hook("pre_llm_call", inject_context)
    logger.info("workflow plugin: registered %d tools + pre_llm_call hook", len(_TOOLS))
