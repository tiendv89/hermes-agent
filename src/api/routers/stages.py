"""Stage-review lifecycle route.

POST /features/{feature_id}/stage-transition — ts lifecycle write (approve/reject/reopen)
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.identity import Identity, require_identity

logger = logging.getLogger(__name__)

router = APIRouter()

_STAGE_ORDER = ["product_spec", "technical_design", "tasks", "handoff"]

_APPROVE_EFFECTS: dict[str, dict] = {
    "product_spec": {
        "feature_status": "in_tdd",
        "current_stage": "technical_design",
        "next_action": "Technical design required. Use the tech-lead skill (Phase 1).",
    },
    "technical_design": {
        "feature_status": "in_tdd",
        "current_stage": "tasks",
        "next_action": "Task breakdown required. Use the tech-lead skill (Phase 2).",
    },
    "tasks": {
        "feature_status": "ready_for_implementation",
        "current_stage": "handoff",
        "next_action": "Tasks ready for implementation.",
    },
    "handoff": {
        "feature_status": "done",
        "current_stage": "done",
        "next_action": "Feature complete.",
    },
}

_REOPEN_EFFECTS: dict[str, dict] = {
    "product_spec": {
        "feature_status": "in_design",
        "current_stage": "product_spec",
        "revalidation": {"technical_design_required": True, "tasks_required": True},
        "next_action": "Product spec reopened. Update the artifact and re-submit for approval.",
    },
    "technical_design": {
        "feature_status": "in_tdd",
        "current_stage": "technical_design",
        "revalidation": {"tasks_required": True},
        "next_action": "Technical design reopened. Update the artifact and re-submit for approval.",
    },
    "tasks": {
        "feature_status": "in_tdd",
        "current_stage": "tasks",
        "revalidation": {},
        "next_action": "Tasks reopened. Update the task breakdown and re-submit for approval.",
    },
    "handoff": {
        "feature_status": "ready_for_implementation",
        "current_stage": "handoff",
        "revalidation": {},
        "next_action": "Handoff reopened. Update the handoff artifact and re-submit.",
    },
}


class StageTransitionRequest(BaseModel):
    stage: str
    action: str  # "approve" | "reject" | "reopen"
    comment: Optional[str] = None


@router.post("/features/{feature_id}/stage-transition")
async def stage_transition_endpoint(
    feature_id: str,
    body: StageTransitionRequest,
    identity: Identity = Depends(require_identity),
) -> JSONResponse:
    """Commit a stage-review state change to status.yaml on the feature branch.

    Only operates on ``ts`` features (those with a ``status.yaml`` in the
    management repo). ``go`` features are out of scope for v3.

    actor = ``X-User-Id`` from ``require_identity``.
    """
    import datetime
    import os as _os

    import yaml

    from plugins.db import _validate_id, get_workspace_context
    from plugins.document_repo import StaleBaseError, read_document, write_document
    from plugins.tools.artifacts import _resolve_management_repo

    _VALID_STAGES = {"product_spec", "technical_design", "tasks", "handoff"}
    _VALID_ACTIONS = {"approve", "reject", "reopen"}

    if body.stage not in _VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage {body.stage!r}. Must be one of {sorted(_VALID_STAGES)}.",
        )
    if body.action not in _VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action {body.action!r}. Must be one of {sorted(_VALID_ACTIONS)}.",
        )

    try:
        _validate_id(feature_id, "feature_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    github_token = _os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        raise HTTPException(
            status_code=500, detail="GITHUB_TOKEN is not configured on the server."
        )

    workspace_id = _os.environ.get("WORKSPACE_ID", "").strip()
    if not workspace_id:
        raise HTTPException(
            status_code=500,
            detail="WORKSPACE_ID is not configured — cannot resolve management repo.",
        )

    try:
        workspace_context = get_workspace_context(workspace_id)
        owner, repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not resolve management repo: {exc}",
        ) from exc

    base_branch = _os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")
    branch = f"feature/{feature_id}"
    path = f"docs/features/{feature_id}/status.yaml"
    actor = identity.user_id

    # Read status.yaml from the feature branch.
    try:
        current = read_document(owner, repo, branch, path, github_token)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read status.yaml: {exc}",
        ) from exc

    if not current["content"]:
        raise HTTPException(
            status_code=404,
            detail=(
                f"status.yaml not found for feature {feature_id!r} on branch {branch!r}. "
                "This endpoint operates on ts features only."
            ),
        )

    try:
        status_data: dict = yaml.safe_load(current["content"])
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not parse status.yaml: {exc}",
        ) from exc

    now = datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+0000"
    )
    stage_block = status_data.setdefault("stages", {}).setdefault(body.stage, {})
    if "review_history" not in stage_block or stage_block["review_history"] is None:
        stage_block["review_history"] = []
    if "history" not in status_data or status_data["history"] is None:
        status_data["history"] = []

    if body.action == "approve":
        stage_block["review_status"] = "approved"
        stage_block["reviewed_by"] = actor
        stage_block["reviewed_at"] = now
        stage_block["review_comment"] = body.comment
        stage_block["review_history"].append(
            {
                "review_status": "approved",
                "reviewed_by": actor,
                "reviewed_at": now,
                "comment": body.comment,
            }
        )
        effects = _APPROVE_EFFECTS.get(body.stage, {})
        if effects:
            status_data["feature_status"] = effects["feature_status"]
            status_data["current_stage"] = effects["current_stage"]
            status_data["next_action"] = effects["next_action"]
        status_data["history"].append(
            {
                "at": now,
                "by": actor,
                "action": "stage_approved",
                "stage": body.stage,
                "note": f"{body.stage} approved by {actor}.",
            }
        )
        commit_msg = f"chore({feature_id}): approve {body.stage} stage"

    elif body.action == "reject":
        stage_block["review_status"] = "rejected"
        stage_block["reviewed_by"] = actor
        stage_block["reviewed_at"] = now
        stage_block["review_comment"] = body.comment
        stage_block["review_history"].append(
            {
                "review_status": "rejected",
                "reviewed_by": actor,
                "reviewed_at": now,
                "comment": body.comment,
            }
        )
        status_data["next_action"] = (
            f"Stage {body.stage} rejected. Address the comment and re-submit for approval."
        )
        status_data["history"].append(
            {
                "at": now,
                "by": actor,
                "action": "stage_rejected",
                "stage": body.stage,
                "note": f"{body.stage} rejected by {actor}. Comment: {body.comment or '(none)'}",
            }
        )
        commit_msg = f"chore({feature_id}): reject {body.stage} stage"

    else:  # reopen
        stage_block["review_status"] = "draft"
        stage_block["reviewed_by"] = None
        stage_block["reviewed_at"] = None
        stage_block["review_comment"] = None
        stage_block["review_history"].append(
            {
                "review_status": "draft",
                "reviewed_by": actor,
                "reviewed_at": now,
                "comment": f"Stage reopened by {actor}.",
            }
        )
        effects = _REOPEN_EFFECTS.get(body.stage, {})
        if effects:
            status_data["feature_status"] = effects["feature_status"]
            status_data["current_stage"] = effects["current_stage"]
            status_data["next_action"] = effects["next_action"]
            revalidation = status_data.setdefault("revalidation", {})
            for k, v in effects.get("revalidation", {}).items():
                revalidation[k] = v
        status_data["history"].append(
            {
                "at": now,
                "by": actor,
                "action": "stage_reopened",
                "stage": body.stage,
                "note": f"{body.stage} reopened by {actor} — artifacts preserved, revalidation flags set.",
            }
        )
        commit_msg = f"chore({feature_id}): reopen {body.stage} stage"

    new_content = yaml.dump(status_data, default_flow_style=False, allow_unicode=True)

    try:
        result = write_document(
            owner,
            repo,
            feature_id,
            base_branch,
            path,
            new_content,
            current["sha"],
            commit_msg,
            github_token,
        )
    except StaleBaseError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "conflict": True,
                "message": "status.yaml changed since you read it. Retry.",
                "detail": str(exc),
            },
        ) from exc
    except Exception as exc:
        logger.exception("stage_transition failed for feature %s", feature_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse(
        {
            "ok": True,
            "feature_id": feature_id,
            "stage": body.stage,
            "action": body.action,
            "review_status": stage_block["review_status"],
            "commit_sha": result["commit_sha"],
            "pr": result["pr"],
        }
    )
