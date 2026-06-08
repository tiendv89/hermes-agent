"""pre_llm_call hook — injects workspace/feature context into the system prompt."""

from __future__ import annotations

import logging
import os
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
                parts.append(
                    "repos: "
                    + ", ".join(
                        r.get("id", r) if isinstance(r, dict) else str(r) for r in repos
                    )
                )

        if feature_id:
            feat = get_feature(workspace_id=workspace_id, feature_id=feature_id)
            if feat.get("ok"):
                parts.append(
                    f"feature_stage: {feat.get('feature', {}).get('stage', 'unknown')}"
                )

            from .tools.tasks import handle as get_tasks

            result = get_tasks(workspace_id=workspace_id, feature_id=feature_id)
            if result.get("ok"):
                t_list = result["tasks"]
                by_status: dict[str, int] = {}
                blocked: list[str] = []
                for t in t_list:
                    by_status[t["status"]] = by_status.get(t["status"], 0) + 1
                    if t["status"] == "blocked" and t.get("blocked_reason"):
                        blocked.append(f"  {t['task_name']}: {t['blocked_reason']}")
                summary = "task_counts: " + ", ".join(
                    f"{k}={v}" for k, v in by_status.items()
                )
                parts.append(summary)
                if blocked:
                    parts.append("blocked_tasks:\n" + "\n".join(blocked))

    caps = ["workflow_get_tasks (live task status)"]
    if os.environ.get("GITNEXUS_MCP_URL"):
        caps.append("workflow_query_gitnexus (code structure / call graph / impact)")
    if os.environ.get("RAG_MCP_URL"):
        caps.append("workflow_query_rag (semantic recall over past specs/designs/logs)")
    parts.append(
        "Before answering questions about task status, code structure, or prior decisions, "
        "use the workflow tools rather than guessing: " + "; ".join(caps) + ". "
        "Draft artifacts through the write tools; never advance lifecycle state directly."
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
