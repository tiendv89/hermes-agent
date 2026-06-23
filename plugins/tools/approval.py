"""request_approval tool.

Surfaces an Approve/Reject/Re-open control card for the human; writes nothing.
The agent calls this to signal that a stage is ready for human review.
The actual state mutation happens via POST /api/v1/features/{id}/stage-transition.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

_VALID_STAGES = frozenset({"product_spec", "technical_design", "tasks", "handoff"})

SCHEMA: Dict[str, Any] = {
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


def _read_review_status(feature_id: str, stage: str) -> str:
    """Return the current review_status for *stage* from status.yaml on the feature branch.

    Returns ``"draft"`` when the file or stage key is absent, ``"unknown"`` on errors.
    """
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    workspace_id = os.environ.get("WORKSPACE_ID", "").strip()
    if not github_token or not workspace_id:
        return "unknown"

    try:
        import yaml

        from ..db import get_feature_detail, get_workspace_context
        from ..document_repo import branch_exists, read_document
        from .artifacts import _resolve_management_repo

        workspace_context = get_workspace_context(workspace_id)
        owner, repo = _resolve_management_repo(workspace_context)

        # All git artifacts are slug-keyed. Resolve the slug and prefer the
        # init branch when the init PR is still open.
        slug = feature_id
        init_pr_url = None
        try:
            detail = get_feature_detail(workspace_id, feature_id)
            slug = detail.get("feature_name") or feature_id
            init_pr_url = detail.get("init_pr_url")
        except Exception:
            pass

        path = f"docs/features/{slug}/status.yaml"
        branch = f"feature/{slug}"
        if init_pr_url:
            init_branch = f"feature/{slug}-init"
            if branch_exists(owner, repo, init_branch, github_token):
                branch = init_branch

        result = read_document(owner, repo, branch, path, github_token)
        if not result["content"]:
            return "draft"

        status_data = yaml.safe_load(result["content"])
        return (
            status_data.get("stages", {})
            .get(stage, {})
            .get("review_status", "draft")
        ) or "draft"
    except Exception as exc:
        logger.warning("_read_review_status failed for %s/%s: %s", feature_id, stage, exc)
        return "unknown"


def handle(stage: str, feature_id: str = "", **_: Any) -> Dict[str, Any]:
    from ..context import get_feature_id

    fid = feature_id or get_feature_id()
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

    review_status = _read_review_status(fid, stage)
    return {
        "ok": True,
        "approval_request": {
            "feature_id": fid,
            "stage": stage,
            "review_status": review_status,
        },
    }
