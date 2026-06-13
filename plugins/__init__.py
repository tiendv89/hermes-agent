"""workflow plugin — registers workspace-aware tools for the digital-factory agent."""

from __future__ import annotations

import logging
from typing import Any

from .db import check_workflow_available
from .hooks import inject_context
from .tools import workspace, feature, artifacts, tasks as tasks_tool, gitnexus, rag

logger = logging.getLogger(__name__)

_TOOLS = (
    {
        "name": "workflow_get_workspace_context",
        "schema": workspace.SCHEMA,
        "handler": workspace.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "workflow_get_feature_state",
        "schema": feature.SCHEMA,
        "handler": feature.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "workflow_write_product_spec",
        "schema": artifacts.WRITE_SPEC_SCHEMA,
        "handler": artifacts.handle_write_product_spec,
        "check_fn": check_workflow_available,
    },
    {
        "name": "workflow_write_technical_design",
        "schema": artifacts.WRITE_TD_SCHEMA,
        "handler": artifacts.handle_write_technical_design,
        "check_fn": check_workflow_available,
    },
    {
        "name": "workflow_get_tasks",
        "schema": tasks_tool.SCHEMA,
        "handler": tasks_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "workflow_query_gitnexus",
        "schema": gitnexus.SCHEMA,
        "handler": gitnexus.handle,
        "check_fn": gitnexus.check_available,
        "is_async": True,
    },
    {
        "name": "workflow_query_rag",
        "schema": rag.SCHEMA,
        "handler": rag.handle,
        "check_fn": rag.check_available,
        "is_async": True,
    },
)


def register(ctx: Any) -> None:
    """Entry point called by PluginManager.discover_and_load."""
    for t in _TOOLS:
        ctx.register_tool(
            name=t["name"],
            toolset="workflow",
            schema=t["schema"],
            handler=t["handler"],
            check_fn=t.get("check_fn"),
            is_async=t.get("is_async", False),
        )
        logger.debug("workflow plugin: registered tool %s", t["name"])

    ctx.register_hook("pre_llm_call", inject_context)
    logger.info("workflow plugin: registered %d tools + pre_llm_call hook", len(_TOOLS))
