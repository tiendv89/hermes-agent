"""workflow plugin — registers workspace-aware tools for the digital-factory agent."""

from __future__ import annotations

import functools
import json
import logging
from typing import Any

from .db import check_workflow_available
from .hooks import inject_context
from .tools import (
    workspace,
    feature,
    artifacts,
    edit as edit_tool,
    read as read_tool,
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


def _json_result_handler(handler: Any, is_async: bool) -> Any:
    """Wrap a tool handler so its return value is JSON-stringified for the model.

    Unpacks the positional args-dict from registry.dispatch into keyword
    arguments before calling the handler, then JSON-encodes the return value.
    The underlying ``handle()`` still returns its dict to direct callers
    (hooks, HTTP routes, unit tests) — only the registered tool handler is
    wrapped.
    """
    if is_async:

        @functools.wraps(handler)
        async def _async_wrapper(*args: Any, **kwargs: Any) -> str:
            return _as_tool_content(await handler(**_unpack_args(args, kwargs)))

        return _async_wrapper

    @functools.wraps(handler)
    def _sync_wrapper(*args: Any, **kwargs: Any) -> str:
        return _as_tool_content(handler(**_unpack_args(args, kwargs)))

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
        "name": "write_product_spec",
        "schema": artifacts.WRITE_SPEC_SCHEMA,
        "handler": artifacts.handle_write_product_spec,
        "check_fn": check_workflow_available,
    },
    {
        "name": "read_document",
        "schema": read_tool.READ_DOCUMENT_SCHEMA,
        "handler": read_tool.handle_read_document,
        "check_fn": check_workflow_available,
    },
    {
        "name": "edit_document",
        "schema": edit_tool.EDIT_DOCUMENT_SCHEMA,
        "handler": edit_tool.handle_edit_document,
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
)


def register(ctx: Any) -> None:
    """Entry point called by PluginManager.discover_and_load."""
    for t in _TOOLS:
        is_async = t.get("is_async", False)
        ctx.register_tool(
            name=t["name"],
            toolset="workflow",
            schema=t["schema"],
            handler=_json_result_handler(t["handler"], is_async),
            check_fn=t.get("check_fn"),
            is_async=is_async,
        )
        logger.debug("workflow plugin: registered tool %s", t["name"])

    ctx.register_hook("pre_llm_call", inject_context)
    logger.info("workflow plugin: registered %d tools + pre_llm_call hook", len(_TOOLS))
