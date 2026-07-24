"""move_feature_status tool — advance a feature out of Backlog into In Design.

A feature is created in ``backlog`` (specs may be drafted there), but its design
cannot be approved until it has been moved to ``in_design`` and reviewed. This
tool performs only that single ``backlog → in_design`` transition. The human
triggers it (e.g. by clicking the "Move to In Design" CTA the agent surfaces
when ``approve_feature`` is called on a backlogged feature); the agent does not
advance the lifecycle on its own.

Status lives in workflow-backend's DB; this tool writes only ``feature_status``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from ..validation import _validate_id

logger = logging.getLogger(__name__)

SCHEMA: dict[str, Any] = {
    "description": (
        "Move a feature out of Backlog into In Design. Use this only when a human "
        "asks to move/advance a feature to In Design (e.g. after clicking the "
        "'Move to In Design' prompt shown when approving a design from Backlog). "
        "It sets feature_status to 'in_design' and touches nothing else — no stage "
        "is approved. If the feature is not in Backlog, it is a safe no-op. "
        "After a successful move, tell the user to review the design and update it "
        "if needed before advancing to Final Design."
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
    from src.services.workflow_backend_client import (
        get_feature_detail,
        run_async,
        update_feature_status,
    )

    from ..context import get_feature_id, get_org_id, get_user_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
    # Capture identity on this (calling) thread — run_async may bridge onto a
    # different thread, where thread-local context is unset.
    caller_user_id = get_user_id()
    caller_org_id = get_org_id()

    if not wid or not fid:
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }

    try:
        _validate_id(fid, "feature_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    actor = os.environ.get("GIT_AUTHOR_EMAIL", os.environ.get("HERMES_ACTOR", "agent"))

    try:
        detail = run_async(
            get_feature_detail(wid, fid, user_id=caller_user_id, org_id=caller_org_id)
        )
    except Exception as exc:
        logger.warning("move_feature_status: could not fetch feature_detail: %s", exc)
        return {"ok": False, "error": f"Could not read feature status: {exc}"}

    current_status = detail.get("status") or ""

    # Idempotent/safe no-op: only backlog features are advanced by this tool.
    if current_status != "backlog":
        return {
            "ok": True,
            "action": "noop",
            "feature_id": fid,
            "feature_status": current_status,
            "note": (
                f"Feature is already past Backlog (status: {current_status or 'unknown'}). "
                "No move performed."
            ),
        }

    try:
        run_async(
            update_feature_status(
                wid,
                fid,
                "in_design",
                actor,
                user_id=caller_user_id,
                org_id=caller_org_id,
            )
        )
    except Exception as exc:
        logger.exception("move_feature_status: update failed for feature %s", fid)
        return {"ok": False, "error": f"Failed to move feature to In Design: {exc}"}

    return {
        "ok": True,
        "action": "moved",
        "feature_id": fid,
        "feature_status": "in_design",
        "next_action": (
            "Feature moved to In Design. Review the design and update it if needed "
            "before advancing to Final Design."
        ),
    }
