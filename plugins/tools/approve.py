"""approve_feature tool — approve, reject, or reopen a feature lifecycle stage.

Mirrors the human-facing POST /features/{id}/stage-transition endpoint but
exposes the action as a callable agent tool so the agent can execute an
approval when a human instructs it to (e.g. "/approve-feature product_spec").

Branch routing follows the same init-branch-first convention used by
_write_artifact: if the feature has an active init PR, status.yaml lives on
feature/{slug}-init; otherwise it is on feature/{uuid}.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Dict, Optional

import yaml

from ..db import _validate_id, get_feature_detail, get_workspace_context
from ..document_repo import StaleBaseError, read_document, write_document
from .artifacts import _resolve_management_repo

logger = logging.getLogger(__name__)

_VALID_STAGES = frozenset({"product_spec", "technical_design", "tasks", "handoff"})
_VALID_ACTIONS = frozenset({"approve", "reject", "reopen"})

_APPROVE_EFFECTS: Dict[str, Dict[str, str]] = {
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

_REOPEN_EFFECTS: Dict[str, Dict] = {
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

SCHEMA: Dict[str, Any] = {
    "description": (
        "Approve, reject, or reopen a feature lifecycle stage. "
        "Only humans may trigger approvals — use this when a human instructs "
        "you to approve or reject a stage (e.g. 'approve the product spec'). "
        "Writes status.yaml on the feature branch and advances the feature "
        "lifecycle accordingly.\n\n"
        "Stage effects on approve:\n"
        "- product_spec → advances to technical_design (feature_status: in_tdd)\n"
        "- technical_design → advances to tasks (feature_status: in_tdd)\n"
        "- tasks → advances to handoff (feature_status: ready_for_implementation)\n"
        "- handoff → marks feature done"
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "stage": {
                "type": "string",
                "enum": ["product_spec", "technical_design", "tasks", "handoff"],
                "description": "The lifecycle stage to act on.",
            },
            "action": {
                "type": "string",
                "enum": ["approve", "reject", "reopen"],
                "default": "approve",
                "description": "Action to perform. Defaults to 'approve'.",
            },
            "comment": {
                "type": "string",
                "description": "Optional comment recorded with reject or reopen actions.",
            },
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier. Omit to use the current workspace from context.",
            },
            "feature_id": {
                "type": "string",
                "description": "Feature identifier. Omit to use the current feature from context.",
            },
        },
        "required": ["stage"],
        "additionalProperties": False,
    },
}


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")


def _resolve_status_branch_and_path(
    gh_owner: str,
    gh_repo: str,
    feature_id: str,
    feature_name: Optional[str],
    init_pr_url: Optional[str],
    base_branch: str,
    github_token: str,
) -> tuple[str, str]:
    """Return (branch, path) for status.yaml.

    Tries the init branch first when the init PR is still open; falls back to
    the feature UUID branch.
    """
    slug = feature_name or feature_id

    if init_pr_url and slug:
        init_branch = f"feature/{slug}-init"
        # Check if init branch exists and has a status.yaml
        from ..document_repo import branch_exists
        if branch_exists(gh_owner, gh_repo, init_branch, github_token):
            return init_branch, f"docs/features/{slug}/status.yaml"

    return f"feature/{feature_id}", f"docs/features/{feature_id}/status.yaml"


def handle(
    stage: str,
    action: str = "approve",
    comment: Optional[str] = None,
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> Dict[str, Any]:
    from ..context import get_feature_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()

    if not wid or not fid:
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }

    if stage not in _VALID_STAGES:
        return {
            "ok": False,
            "error": f"Invalid stage {stage!r}. Must be one of {sorted(_VALID_STAGES)}.",
        }

    action = (action or "approve").lower()
    if action not in _VALID_ACTIONS:
        return {
            "ok": False,
            "error": f"Invalid action {action!r}. Must be one of {sorted(_VALID_ACTIONS)}.",
        }

    try:
        _validate_id(fid, "feature_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    actor = os.environ.get("GIT_AUTHOR_EMAIL", os.environ.get("HERMES_ACTOR", "agent"))

    try:
        workspace_context = get_workspace_context(wid)
        gh_owner, gh_repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        return {"ok": False, "error": f"Could not resolve management repo: {exc}"}

    base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")

    # Look up feature name and init PR URL for branch resolution.
    feature_name: Optional[str] = None
    init_pr_url: Optional[str] = None
    try:
        detail = get_feature_detail(wid, fid)
        feature_name = detail.get("feature_name")
        init_pr_url = detail.get("init_pr_url")
    except Exception as exc:
        logger.debug("approve_feature: could not fetch feature_detail: %s", exc)

    branch, path = _resolve_status_branch_and_path(
        gh_owner, gh_repo, fid, feature_name, init_pr_url, base_branch, github_token
    )

    try:
        current = read_document(gh_owner, gh_repo, branch, path, github_token)
    except Exception as exc:
        return {"ok": False, "error": f"Could not read status.yaml: {exc}"}

    if not current["content"]:
        return {
            "ok": False,
            "error": (
                f"status.yaml not found on branch {branch!r} at {path!r}. "
                "Ensure the feature was initialized with the init PR flow."
            ),
        }

    try:
        status_data: dict = yaml.safe_load(current["content"])
    except Exception as exc:
        return {"ok": False, "error": f"Could not parse status.yaml: {exc}"}

    now = _now_utc()
    stage_block = status_data.setdefault("stages", {}).setdefault(stage, {})
    if not isinstance(stage_block.get("review_history"), list):
        stage_block["review_history"] = []
    if not isinstance(status_data.get("history"), list):
        status_data["history"] = []

    if action == "approve":
        stage_block["review_status"] = "approved"
        stage_block["reviewed_by"] = actor
        stage_block["reviewed_at"] = now
        stage_block["review_comment"] = comment
        stage_block["review_history"].append({"review_status": "approved", "reviewed_by": actor, "reviewed_at": now, "comment": comment})
        effects = _APPROVE_EFFECTS.get(stage, {})
        if effects:
            status_data["feature_status"] = effects["feature_status"]
            status_data["current_stage"] = effects["current_stage"]
            status_data["next_action"] = effects["next_action"]
        status_data["history"].append({"at": now, "by": actor, "action": "stage_approved", "stage": stage, "note": f"{stage} approved by {actor}."})
        commit_msg = f"chore: approve {stage} stage"

    elif action == "reject":
        stage_block["review_status"] = "rejected"
        stage_block["reviewed_by"] = actor
        stage_block["reviewed_at"] = now
        stage_block["review_comment"] = comment
        stage_block["review_history"].append({"review_status": "rejected", "reviewed_by": actor, "reviewed_at": now, "comment": comment})
        status_data["next_action"] = f"Stage {stage} rejected. Address the comment and re-submit for approval."
        status_data["history"].append({"at": now, "by": actor, "action": "stage_rejected", "stage": stage, "note": f"{stage} rejected by {actor}. Comment: {comment or '(none)'}"})
        commit_msg = f"chore: reject {stage} stage"

    else:  # reopen
        stage_block["review_status"] = "draft"
        stage_block["reviewed_by"] = None
        stage_block["reviewed_at"] = None
        stage_block["review_comment"] = None
        stage_block["review_history"].append({"review_status": "draft", "reviewed_by": actor, "reviewed_at": now, "comment": f"Stage reopened by {actor}."})
        effects = _REOPEN_EFFECTS.get(stage, {})
        if effects:
            status_data["feature_status"] = effects["feature_status"]
            status_data["current_stage"] = effects["current_stage"]
            status_data["next_action"] = effects["next_action"]
            revalidation = status_data.setdefault("revalidation", {})
            for k, v in effects.get("revalidation", {}).items():
                revalidation[k] = v
        status_data["history"].append({"at": now, "by": actor, "action": "stage_reopened", "stage": stage, "note": f"{stage} reopened by {actor}."})
        commit_msg = f"chore: reopen {stage} stage"

    new_content = yaml.dump(status_data, default_flow_style=False, allow_unicode=True)

    try:
        result = write_document(
            gh_owner, gh_repo, fid, base_branch, path, new_content,
            current["sha"], commit_msg, github_token,
        )
    except StaleBaseError as exc:
        return {"ok": False, "conflict": True, "error": f"status.yaml changed since it was read. Retry. ({exc})"}
    except Exception as exc:
        logger.exception("approve_feature: write_document failed for feature %s", fid)
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "feature_id": fid,
        "stage": stage,
        "action": action,
        "review_status": stage_block["review_status"],
        "feature_status": status_data.get("feature_status"),
        "current_stage": status_data.get("current_stage"),
        "commit_sha": result.get("commit_sha", ""),
        "branch": branch,
    }
