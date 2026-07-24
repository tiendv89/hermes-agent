"""get_tasks tool — returns live task status from the workspace database."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA: dict[str, Any] = {
    "description": (
        "Return the live status of every task in the current feature, sourced from the "
        "database (status, blocked_reason, PR url, depends_on, actor). Call this to answer "
        "questions like 'which tasks are blocked / in progress / done' or 'what's left'. "
        "Omit workspace_id/feature_id to use the current feature from context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier. Omit to use the current workspace from context.",
            },
            "feature_id": {
                "type": "string",
                "description": "Feature identifier. Omit to use the current feature from context.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}


def handle(workspace_id: str = "", feature_id: str = "", **_: Any) -> dict[str, Any]:
    from src.services.workflow_backend_client import get_feature_tasks, run_async

    from ..context import get_feature_id, get_org_id, get_user_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
    if not wid or not fid:
        return {"ok": False, "error": "workspace_id and feature_id are required but were not provided and no context is set."}
    try:
        tasks = run_async(get_feature_tasks(wid, fid, user_id=get_user_id(), org_id=get_org_id()))
        return {"ok": True, "tasks": tasks}
    except Exception as exc:
        logger.warning("get_tasks failed: %s", exc)
        return {"ok": False, "error": str(exc)}
