"""workflow_get_workspace_context tool — reads workspace metadata from the workflow-backend DB."""

from __future__ import annotations

import logging
from typing import Any, Dict

from ..db import get_workspace_context

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {
            "type": "string",
            "description": "The workspace identifier (slug or UUID).",
        },
    },
    "required": ["workspace_id"],
    "additionalProperties": False,
}


def handle(workspace_id: str, **_: Any) -> Dict[str, Any]:
    try:
        return {"ok": True, "workspace": get_workspace_context(workspace_id)}
    except Exception as exc:
        logger.warning("workflow_get_workspace_context failed: %s", exc)
        return {"ok": False, "error": str(exc)}
