"""workflow plugin — registers workspace-aware tools for the digital-factory agent.

Exposes four tools (read tools v1, write tools placeholder for T5):
    workflow_get_workspace_context
    workflow_get_feature_state
    workflow_write_product_spec       (stub — implemented in T5)
    workflow_write_technical_design   (stub — implemented in T5)

Also registers a ``pre_llm_call`` hook that injects workspace + feature context
into the system prompt of every turn so the agent always has current state.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .tools import (
    WS_CONTEXT_SCHEMA,
    FEATURE_STATE_SCHEMA,
    WRITE_SPEC_SCHEMA,
    WRITE_TD_SCHEMA,
    check_workflow_available,
    handle_get_workspace_context,
    handle_get_feature_state,
    handle_write_product_spec,
    handle_write_technical_design,
)

logger = logging.getLogger(__name__)

_TOOLS = (
    ("workflow_get_workspace_context", WS_CONTEXT_SCHEMA, handle_get_workspace_context),
    ("workflow_get_feature_state", FEATURE_STATE_SCHEMA, handle_get_feature_state),
    ("workflow_write_product_spec", WRITE_SPEC_SCHEMA, handle_write_product_spec),
    ("workflow_write_technical_design", WRITE_TD_SCHEMA, handle_write_technical_design),
)


def _inject_feature_context(messages: list, **kwargs: Any) -> None:
    """pre_llm_call hook — prepend a workspace/feature context block to system.

    The hook reads ``workspace_id`` and ``feature_id`` from the agent's
    ``context_vars`` (injected by the gateway before each turn). If either is
    missing the hook is a no-op so the agent still works outside the gateway.
    """
    context_vars: dict = kwargs.get("context_vars") or {}
    workspace_id: str = context_vars.get("workspace_id", "")
    feature_id: str = context_vars.get("feature_id", "")

    if not workspace_id:
        return

    parts: list[str] = [
        "## Workflow context (injected by workflow plugin)",
        f"workspace_id: {workspace_id}",
    ]

    if feature_id:
        parts.append(f"feature_id: {feature_id}")

    if check_workflow_available():
        from .tools import handle_get_workspace_context, handle_get_feature_state

        ws_result = handle_get_workspace_context(workspace_id=workspace_id)
        if ws_result.get("ok"):
            workspace = ws_result.get("workspace", {})
            repos = workspace.get("repos", [])
            if repos:
                parts.append("repos: " + ", ".join(r.get("id", r) if isinstance(r, dict) else str(r) for r in repos))

        if feature_id:
            feat_result = handle_get_feature_state(workspace_id=workspace_id, feature_id=feature_id)
            if feat_result.get("ok"):
                feature = feat_result.get("feature", {})
                stage = feature.get("stage", "unknown")
                parts.append(f"feature_stage: {stage}")

    parts.append(
        "Instructions: Draft artifacts through the workflow tools. "
        "Never advance lifecycle state directly. "
        "The human approves via the existing approval flow."
    )

    context_block = "\n".join(parts)

    # Inject into the first system message if one exists; otherwise prepend one.
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            existing = msg.get("content", "")
            if isinstance(existing, str):
                msg["content"] = context_block + "\n\n" + existing
            elif isinstance(existing, list):
                existing.insert(0, {"type": "text", "text": context_block + "\n\n"})
            return

    messages.insert(0, {"role": "system", "content": context_block})


def register(ctx: Any) -> None:
    """Entry point called by PluginManager.discover_and_load."""
    for name, schema, handler in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset="workflow",
            schema=schema,
            handler=handler,
            check_fn=check_workflow_available,
        )
        logger.debug("workflow plugin: registered tool %s", name)

    ctx.register_hook("pre_llm_call", _inject_feature_context)
    logger.info("workflow plugin: registered %d tools + pre_llm_call hook", len(_TOOLS))
