"""workflow_get_feature_state tool — reads feature lifecycle state from the workflow-backend DB."""

from __future__ import annotations

import logging
from typing import Any, Dict

from ..db import get_feature_detail

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {
            "type": "string",
            "description": "The workspace identifier (slug or UUID).",
        },
        "feature_id": {
            "type": "string",
            "description": "The feature identifier (name or UUID).",
        },
    },
    "required": ["workspace_id", "feature_id"],
    "additionalProperties": False,
}


def handle(workspace_id: str, feature_id: str, **_: Any) -> Dict[str, Any]:
    try:
        return {"ok": True, "feature": get_feature_detail(workspace_id, feature_id)}
    except Exception as exc:
        logger.warning("workflow_get_feature_state failed: %s", exc)
        return {"ok": False, "error": str(exc)}
