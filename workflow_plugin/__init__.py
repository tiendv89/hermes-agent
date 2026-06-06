"""workflow plugin — registers workspace-aware tools for the digital-factory agent."""

from __future__ import annotations

import logging
from typing import Any

from .db import check_workflow_available
from .hooks import inject_context
from .tools import workspace, feature, artifacts

logger = logging.getLogger(__name__)

_TOOLS = (
    ("workflow_get_workspace_context",  workspace.SCHEMA,           workspace.handle),
    ("workflow_get_feature_state",      feature.SCHEMA,             feature.handle),
    ("workflow_write_product_spec",     artifacts.WRITE_SPEC_SCHEMA, artifacts.handle_write_product_spec),
    ("workflow_write_technical_design", artifacts.WRITE_TD_SCHEMA,   artifacts.handle_write_technical_design),
)


def register(ctx: Any) -> None:
    """Entry point called by PluginManager.discover_and_load."""
    for name, schema, handler in _TOOLS:
        ctx.register_tool(name=name, toolset="workflow", schema=schema, handler=handler,
                          check_fn=check_workflow_available)
        logger.debug("workflow plugin: registered tool %s", name)

    ctx.register_hook("pre_llm_call", inject_context)
    logger.info("workflow plugin: registered %d tools + pre_llm_call hook", len(_TOOLS))
