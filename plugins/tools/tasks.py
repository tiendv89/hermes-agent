"""get_tasks tool — returns live task status from the workspace database."""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Return the live status of every task in the current feature, sourced from the "
        "database (status, blocked_reason, PR url, depends_on, actor). Call this to answer "
        "questions like 'which tasks are blocked / in progress / done' or 'what's left'. "
        "Omit workspace_id/feature_id to use the current feature from context."
    ),
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
}


def handle(workspace_id: str = "", feature_id: str = "", **_: Any) -> Dict[str, Any]:
    from ..context import get_feature_id, get_workspace_id
    from ..db import get_feature_tasks

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
    if not wid or not fid:
        return {"ok": False, "error": "workspace_id and feature_id are required but were not provided and no context is set."}
    try:
        return {"ok": True, "tasks": get_feature_tasks(wid, fid)}
    except Exception as exc:
        logger.warning("get_tasks failed: %s", exc)
        return {"ok": False, "error": str(exc)}
