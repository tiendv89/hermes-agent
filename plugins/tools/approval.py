"""request_approval tool.

Surfaces an Approve/Reject/Re-open control card for the human; writes nothing.
The agent calls this to signal that a stage is ready for human review.
The actual state mutation happens via the approve_feature tool.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_VALID_STAGES = frozenset({"product_spec", "technical_design", "tasks", "handoff"})

SCHEMA: dict[str, Any] = {
    "description": (
        "Request human approval for a feature lifecycle stage. "
        "Surfaces an Approve / Reject / Re-open card to the user — "
        "this tool does NOT approve or modify any state itself. "
        "The human's decision is applied via the stage-transition endpoint."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "feature_id": {
                "type": "string",
                "description": "Feature identifier. Omit to use the current feature from context.",
            },
            "stage": {
                "type": "string",
                "enum": ["product_spec", "technical_design", "tasks", "handoff"],
                "description": "The lifecycle stage to request approval for.",
            },
        },
        "required": ["stage"],
        "additionalProperties": False,
    },
}


def _read_review_status(workspace_id: str, feature_id: str, stage: str) -> str:
    """Return the current review_status for *stage* from workflow-backend's feature detail.

    Returns ``"draft"`` when the stage key is absent, ``"unknown"`` on errors.
    """
    if not workspace_id:
        return "unknown"

    try:
        from src.services.workflow_backend_client import get_feature_detail, run_async

        from ..context import get_org_id, get_user_id

        caller_user_id = get_user_id()
        caller_org_id = get_org_id()

        detail = run_async(get_feature_detail(workspace_id, feature_id, user_id=caller_user_id, org_id=caller_org_id))
        return (
            (detail.get("stages") or {})
            .get(stage, {})
            .get("review_status", "draft")
        ) or "draft"
    except Exception as exc:
        logger.warning("_read_review_status failed for %s/%s: %s", feature_id, stage, exc)
        return "unknown"


def handle(stage: str, feature_id: str = "", **_: Any) -> dict[str, Any]:
    from ..context import get_feature_id, get_workspace_id

    fid = feature_id or get_feature_id()
    wid = get_workspace_id()
    if not fid:
        return {
            "ok": False,
            "error": "feature_id is required but was not provided and no context is set.",
        }

    if stage not in _VALID_STAGES:
        return {
            "ok": False,
            "error": f"Invalid stage {stage!r}. Must be one of {sorted(_VALID_STAGES)}.",
        }

    review_status = _read_review_status(wid, fid, stage)
    return {
        "ok": True,
        "approval_request": {
            "feature_id": fid,
            "stage": stage,
            "review_status": review_status,
        },
    }
