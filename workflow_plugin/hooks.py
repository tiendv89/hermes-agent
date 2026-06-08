"""pre_llm_call hook — returns workspace/feature context for injection into the user message."""

from __future__ import annotations

import logging
import os
from typing import Any

from .context import get_context_for_session
from .db import check_workflow_available

logger = logging.getLogger(__name__)


def inject_context(session_id: str = "", **kwargs: Any) -> dict | None:
    """Build a workspace/feature context block and return it for injection.

    The conversation loop appends the returned {"context": "..."} string to the
    current turn's user message.  workspace_id and feature_id are resolved from
    the per-session store the gateway router populates before each
    agent.run_conversation() call (keyed by the session_id the hook receives).
    """
    workspace_id, feature_id = get_context_for_session(session_id)

    if not workspace_id:
        logger.info(
            "workflow inject_context: no workspace context for session=%s — skipping injection",
            session_id,
        )
        return None

    parts = [
        "## Workflow context",
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
                f = feat["feature"]
                if f.get("title"):
                    parts.append(f"feature_title: {f['title']}")
                if f.get("feature_name"):
                    parts.append(f"feature_name: {f['feature_name']}")
                parts.append(f"feature_stage: {f.get('stage', 'unknown')}")
                parts.append(f"feature_status: {f.get('status', 'unknown')}")
                if f.get("next_action"):
                    parts.append(f"next_action: {f['next_action']}")

            from .tools.tasks import handle as get_tasks

            result = get_tasks(workspace_id=workspace_id, feature_id=feature_id)
            if result.get("ok"):
                t_list = result["tasks"]
                task_lines: list[str] = []
                for t in t_list:
                    name = t.get("task_name", "")
                    title = t.get("title", "")
                    status = t.get("status", "")
                    line = f"  {name}: {title} [{status}]"
                    if t["status"] == "blocked" and t.get("blocked_reason"):
                        line += f" — blocked: {t['blocked_reason']}"
                    task_lines.append(line)
                if task_lines:
                    parts.append("tasks:\n" + "\n".join(task_lines))

    caps = ["workflow_get_tasks (live task status)"]
    if os.environ.get("GITNEXUS_MCP_URL"):
        caps.append("workflow_query_gitnexus (code structure / call graph / impact)")
    if os.environ.get("RAG_MCP_URL"):
        caps.append("workflow_query_rag (semantic recall over past specs/designs/logs)")
    parts.append(
        "Before answering questions about task status, code structure, or prior decisions, "
        "use the workflow tools rather than guessing: " + "; ".join(caps) + ". "
        "Workspace and feature IDs are already set in context — omit them when calling tools."
    )

    return {"context": "\n".join(parts)}
