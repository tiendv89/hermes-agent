"""get_workspace_context tool — reads workspace metadata from the workflow-backend DB."""

from __future__ import annotations

import logging
from typing import Any, Dict

from ..db import get_workspace_context

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "description": (
        "Read a workspace's context — its repos, roles, environments and "
        "workflow settings from workspace.yaml. Use this to learn which repos "
        "and stacks a feature spans before designing or breaking down work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier (slug or UUID). Omit to use the current workspace from context.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}


def handle(workspace_id: str = "", **_: Any) -> Dict[str, Any]:
    from ..context import get_workspace_id

    wid = workspace_id or get_workspace_id()
    if not wid:
        return {"ok": False, "error": "workspace_id is required but was not provided and no workspace context is set."}
    try:
        return {"ok": True, "workspace": get_workspace_context(wid)}
    except Exception as exc:
        logger.warning("get_workspace_context failed: %s", exc)
        return {"ok": False, "error": str(exc)}
