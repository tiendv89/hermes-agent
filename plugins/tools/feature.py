"""get_feature_state tool — reads feature lifecycle state from workflow-backend."""

from __future__ import annotations

import logging
from typing import Any, Dict

from src.services.workflow_backend_client import get_feature_detail, run_async

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "description": (
        "Return full feature metadata (title, stage, status, next_action). "
        "Omit workspace_id/feature_id to use the current feature from context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier (slug or UUID). Omit to use the current workspace from context.",
            },
            "feature_id": {
                "type": "string",
                "description": "Feature identifier (name or UUID). Omit to use the current feature from context.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}


def handle(workspace_id: str = "", feature_id: str = "", **_: Any) -> Dict[str, Any]:
    from ..context import get_feature_id, get_org_id, get_user_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
    if not wid or not fid:
        return {"ok": False, "error": "workspace_id and feature_id are required but were not provided and no context is set."}
    try:
        feature = run_async(get_feature_detail(wid, fid, user_id=get_user_id(), org_id=get_org_id()))
        return {"ok": True, "feature": feature}
    except Exception as exc:
        logger.warning("get_feature_state failed: %s", exc)
        return {"ok": False, "error": str(exc)}
