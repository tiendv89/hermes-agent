"""workflow_get_tasks tool — returns live task status from the workspace database."""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Return the live status of every task in the current feature, sourced from the "
        "database (status, blocked_reason, PR url, depends_on, actor). Call this to answer "
        "questions like 'which tasks are blocked / in progress / done' or 'what's left'."
    ),
    "properties": {
        "workspace_id": {"type": "string"},
        "feature_id": {"type": "string"},
    },
    "required": ["workspace_id", "feature_id"],
    "additionalProperties": False,
}


def handle(workspace_id: str, feature_id: str, **_: Any) -> Dict[str, Any]:
    from ..db import get_feature_tasks

    try:
        return {"ok": True, "tasks": get_feature_tasks(workspace_id, feature_id)}
    except Exception as exc:
        logger.warning("workflow_get_tasks failed: %s", exc)
        return {"ok": False, "error": str(exc)}
