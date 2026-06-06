"""pre_llm_call hook — injects workspace/feature context into the system prompt."""

from __future__ import annotations

import logging
from typing import Any

from .db import check_workflow_available

logger = logging.getLogger(__name__)


def inject_context(messages: list, **kwargs: Any) -> None:
    """Prepend a workspace/feature context block to the system prompt each turn.

    Reads workspace_id and feature_id from the agent's context_vars (injected
    by the gateway). No-op when workspace_id is absent so the agent works
    outside the gateway too.
    """
    context_vars: dict = kwargs.get("context_vars") or {}
    workspace_id: str = context_vars.get("workspace_id", "")
    feature_id: str = context_vars.get("feature_id", "")

    if not workspace_id:
        return

    parts = [
        "## Workflow context (injected by workflow plugin)",
        f"workspace_id: {workspace_id}",
    ]
    if feature_id:
        parts.append(f"feature_id: {feature_id}")

    if check_workflow_available():
        from .tools.workspace import handle as get_workspace
        from .tools.feature import handle as get_feature

        ws = get_workspace(workspace_id=workspace_id)
        if ws.get("ok"):
            repos = ws.get("workspace", {}).get("repos", [])
            if repos:
                parts.append("repos: " + ", ".join(
                    r.get("id", r) if isinstance(r, dict) else str(r) for r in repos
                ))

        if feature_id:
            feat = get_feature(workspace_id=workspace_id, feature_id=feature_id)
            if feat.get("ok"):
                parts.append(f"feature_stage: {feat.get('feature', {}).get('stage', 'unknown')}")

    parts.append(
        "Instructions: Draft artifacts through the workflow tools. "
        "Never advance lifecycle state directly. "
        "The human approves via the existing approval flow."
    )

    block = "\n".join(parts)

    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "system":
            existing = msg.get("content", "")
            if isinstance(existing, str):
                msg["content"] = block + "\n\n" + existing
            elif isinstance(existing, list):
                existing.insert(0, {"type": "text", "text": block + "\n\n"})
            return

    messages.insert(0, {"role": "system", "content": block})
