"""approve_feature tool — approve, reject, or reopen a feature lifecycle stage.

Exposes the action as a callable agent tool so the agent can execute an
approval when a human instructs it to (e.g. "/approve-feature product_spec").
Status lives in workflow-backend's DB; documents live in storage-service.
"""

from __future__ import annotations

import datetime
import logging
import os
from typing import Any, Dict, List, Optional

from plugins.clients.storage_service_client import read_document_content
from ..validation import _validate_id
from src.services.approval_notifications import notify_stage_approved
from src.services.workflow_backend_client import (
    activate_ready_tasks,
    get_feature_detail,
    run_async,
    update_feature_stage,
)

logger = logging.getLogger(__name__)


def _relay_go_reason_code(reason_code: str) -> str:
    """Return human-readable guidance for a workflow-backend reason code."""
    if reason_code == "feature_not_tasks_approved":
        return (
            "The feature is not in the tasks-approved state yet. "
            "Ensure the tasks stage was approved and the docs PR was merged, then retry."
        )
    if reason_code == "missing_config":
        return (
            "Missing configuration: WORKFLOW_BACKEND_URL or WORKFLOW_BACKEND_SERVICE_TOKEN "
            "is not set. Contact your platform team to provision these values."
        )
    if reason_code == "empty_tasks":
        return (
            "No tasks found in the tasks.md Index table. Verify that tasks.md has a "
            "valid Index table with columns | ID | Title | Repo | Depends On | Actor | "
            "and at least one T<n> row."
        )
    return "An unexpected error occurred when creating tasks in workflow-backend."


def _run_async_create_tasks(workspace_id: str, feature_id: str, tasks: list) -> dict:
    """Bridge the async create_feature_tasks coroutine into a sync call.

    ``tasks`` is an already-parsed task list (see ``parse_tasks_index``); this
    bridge no longer parses tasks.md.
    """
    from plugins.context import get_org_id, get_user_id
    from src.services.workflow_backend_client import create_feature_tasks, run_async

    # Capture caller identity on THIS (worker) thread. When the coroutine is
    # scheduled on the agent loop (a different thread), thread-local identity is
    # not visible there — so resolve it here and pass it through explicitly.
    user_id = get_user_id()
    org_id = get_org_id()

    return run_async(
        create_feature_tasks(workspace_id, feature_id, tasks, user_id=user_id, org_id=org_id)
    )


_VALID_STAGES = frozenset({"product_spec", "technical_design", "tasks", "handoff"})
_VALID_ACTIONS = frozenset({"approve", "reject", "reopen"})

_APPROVE_EFFECTS: Dict[str, Dict[str, str]] = {
    "product_spec": {
        "feature_status": "in_tdd",
        "current_stage": "technical_design",
        "next_action": "Technical design required. Write the technical design next.",
    },
    "technical_design": {
        "feature_status": "in_tdd",
        "current_stage": "tasks",
        "next_action": "Task breakdown required. Write the task breakdown next.",
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
        "Updates the feature's status in workflow-backend and advances the "
        "feature lifecycle accordingly.\n\n"
        "IMPORTANT: Always call this tool even if you believe the stage is already "
        "approved. When stage=tasks, the tool will activate any tasks that are still "
        "todo but have their dependencies met — it handles the already-approved case "
        "safely (no duplicate commit, just task activation).\n\n"
        "Stage effects on approve:\n"
        "- product_spec → advances to technical_design (feature_status: in_tdd)\n"
        "- technical_design → advances to tasks (feature_status: in_tdd)\n"
        "- tasks → advances to handoff (feature_status: ready_for_implementation) "
        "AND activates zero-dependency tasks to ready\n"
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


def _activate_tasks_db(workspace_id: str, feature_id: str, actor: str) -> list:
    """Set zero-dependency (or now-unblocked) tasks to ready in workspace_tasks."""
    from ..context import get_org_id, get_user_id

    return run_async(
        activate_ready_tasks(workspace_id, feature_id, user_id=get_user_id(), org_id=get_org_id())
    )


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+0000"
    )


def handle(
    stage: str,
    action: str = "approve",
    comment: Optional[str] = None,
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> Dict[str, Any]:
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

    actor = os.environ.get("GIT_AUTHOR_EMAIL", os.environ.get("HERMES_ACTOR", "agent"))

    detail: Dict[str, Any] = {}
    try:
        detail = run_async(get_feature_detail(wid, fid, user_id=caller_user_id, org_id=caller_org_id))
    except Exception as exc:
        logger.debug("approve_feature: could not fetch feature_detail: %s", exc)

    # Status lives entirely in workflow-backend's DB.
    status_data: dict = {
        "feature_status": detail.get("status") or "",
        "current_stage": detail.get("stage") or "",
        "next_action": detail.get("next_action") or "",
        "stages": dict(detail.get("stages") or {}),
    }

    now = _now_utc()
    stage_block = status_data.setdefault("stages", {}).setdefault(stage, {})
    if not isinstance(stage_block.get("review_history"), list):
        stage_block["review_history"] = []
    if not isinstance(status_data.get("history"), list):
        status_data["history"] = []

    # Fast-path: stage already approved — skip the status write but still run
    # task activation for stage=tasks so any unactivated tasks get set to ready.
    # Exception: tasks-approve falls through so tasks get (re-)created/activated
    # (resumable pipeline).
    already_approved = (
        stage_block.get("review_status") == "approved" and action == "approve"
    )
    _is_tasks_approve = stage == "tasks" and action == "approve"
    if already_approved and not _is_tasks_approve:
        activated_tasks: list = []
        if stage == "tasks":
            try:
                activated_tasks = _activate_tasks_db(wid, fid, actor)
            except Exception as exc:
                logger.warning(
                    "approve_feature (already-approved fast-path): DB activation failed: %s",
                    exc,
                )
        return {
            "ok": True,
            "feature_id": fid,
            "stage": stage,
            "action": "noop",
            "review_status": "approved",
            "feature_status": status_data.get("feature_status", ""),
            "current_stage": status_data.get("current_stage", ""),
            "commit_sha": "",
            "branch": None,
            "activated_tasks": activated_tasks,
            "note": (
                f"Stage '{stage}' was already approved — status unchanged. "
                + (
                    f"Activated {len(activated_tasks)} task(s): {activated_tasks}."
                    if activated_tasks
                    else "No additional tasks to activate (all are already ready/in-progress/done)."
                )
            ),
        }
    # If already_approved and _is_tasks_approve: fall through — task creation/activation still runs.

    if action == "approve":
        stage_block["review_status"] = "approved"
        stage_block["reviewed_by"] = actor
        stage_block["reviewed_at"] = now
        stage_block["review_comment"] = comment
        stage_block["review_history"].append(
            {
                "review_status": "approved",
                "reviewed_by": actor,
                "reviewed_at": now,
                "comment": comment,
            }
        )
        effects = _APPROVE_EFFECTS.get(stage, {})
        if effects:
            status_data["feature_status"] = effects["feature_status"]
            status_data["current_stage"] = effects["current_stage"]
            status_data["next_action"] = effects["next_action"]
        status_data["history"].append(
            {
                "at": now,
                "by": actor,
                "action": "stage_approved",
                "stage": stage,
                "note": f"{stage} approved by {actor}.",
            }
        )

    elif action == "reject":
        stage_block["review_status"] = "rejected"
        stage_block["reviewed_by"] = actor
        stage_block["reviewed_at"] = now
        stage_block["review_comment"] = comment
        stage_block["review_history"].append(
            {
                "review_status": "rejected",
                "reviewed_by": actor,
                "reviewed_at": now,
                "comment": comment,
            }
        )
        status_data["next_action"] = (
            f"Stage {stage} rejected. Address the comment and re-submit for approval."
        )
        status_data["history"].append(
            {
                "at": now,
                "by": actor,
                "action": "stage_rejected",
                "stage": stage,
                "note": f"{stage} rejected by {actor}. Comment: {comment or '(none)'}",
            }
        )

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
        effects = _REOPEN_EFFECTS.get(stage, {})
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
                "stage": stage,
                "note": f"{stage} reopened by {actor}.",
            }
        )

    new_feature_status = status_data.get("feature_status", "")
    new_current_stage = status_data.get("current_stage", "")
    new_next_action = status_data.get("next_action", "")

    commit_sha = ""
    activated_tasks: list = []
    model_confirmation: List[Dict[str, Any]] = []

    if _is_tasks_approve:
        # tasks-approve pipeline: status lives entirely in workflow-backend's
        # DB and docs live in storage-service, so this collapses to: DB status
        # update, then create/activate tasks.

        # Step c: DB status update (idempotent set).
        try:
            run_async(
                update_feature_stage(
                    workspace_id=wid,
                    feature_id=fid,
                    stage=stage,
                    review_status=stage_block["review_status"],
                    feature_status=new_feature_status,
                    current_stage=new_current_stage,
                    next_action=new_next_action,
                    actor=actor,
                    user_id=caller_user_id,
                    org_id=caller_org_id,
                )
            )
        except Exception as exc:
            logger.exception(
                "approve_feature: step c DB update failed for feature %s", fid
            )
            return {
                "ok": False,
                "error": f"Step c (DB status): {exc}. Re-run approve to retry.",
                "failed_step": "c",
            }

        # Step d: create tasks via workflow-backend API.
        # tasks.md for go-owned features lives in storage-service, not git (see
        # provisionStorageDocuments in workflow-backend's feature_create.go).
        try:
            tasks_md_result = read_document_content(
                wid, fid, "tasks.md", user_id=caller_user_id, org_id=caller_org_id
            )
        except Exception as exc:
            return {
                "ok": False,
                "error": (
                    f"Step d (create tasks): could not read tasks.md from "
                    f"storage-service: {exc}. Re-run approve to retry."
                ),
                "failed_step": "d",
            }
        tasks_md_content = tasks_md_result.get("content", "")
        if not tasks_md_content:
            return {
                "ok": False,
                "error": (
                    f"Step d (create tasks): tasks.md not found in storage-service "
                    f"for feature {fid!r}. Re-run approve to retry."
                ),
                "failed_step": "d",
            }

        from .parse_tasks import parse_tasks_index

        tasks = parse_tasks_index(tasks_md_content)
        if not tasks:
            return {
                "ok": False,
                "error": (
                    "Step d (create tasks): no tasks parsed from tasks.md. It must "
                    "contain an Index table with columns "
                    "| ID | Title | Repo | Depends On | Actor | Model | and at least one "
                    "T<n> row. Fix tasks.md and re-run approve to retry."
                ),
                "failed_step": "d",
            }

        # Confirmation preview + model resolution (product spec Goal 4 / §6a).
        # Re-calls the candidates endpoint to resolve each agent-actor task's
        # stored display name to a model_id UUID before sending to create_tasks.
        # On any unresolved display name: abort step d and surface the failure so
        # the user can correct the model selection in tasks.md and retry.
        from .model_resolution import format_unresolved_error, resolve_task_models

        resolution = resolve_task_models(
            wid, tasks, user_id=caller_user_id or "", org_id=caller_org_id or ""
        )
        if not resolution["ok"]:
            return {
                "ok": False,
                "error": (
                    f"Step d (create tasks): "
                    + format_unresolved_error(resolution["unresolved"])
                ),
                "failed_step": "d",
                "unresolved_models": resolution["unresolved"],
            }
        resolved_tasks = resolution["tasks"]

        # Build the confirmation preview for the response (every agent task with
        # its resolved model display name — shown to the human before tasks are
        # actually created, per product spec Goal 4).
        model_confirmation: List[Dict[str, Any]] = [
            {
                "task_name": t["name"],
                "title": t.get("title", ""),
                "model": t.get("model", ""),
                "model_id": t.get("model_id", ""),
            }
            for t in resolved_tasks
            if t.get("actor_type") == "agent"
        ]

        from src.services.workflow_backend_client import WorkflowBackendError as _WBE

        try:
            _run_async_create_tasks(wid, fid, resolved_tasks)
        except _WBE as exc:
            if exc.reason_code == "tasks_already_exist":
                logger.info(
                    "approve_feature: step d tasks already exist for feature %s — no-op",
                    fid,
                )
            else:
                relay = _relay_go_reason_code(exc.reason_code)
                return {
                    "ok": False,
                    "error": (
                        f"Step d (create tasks): {relay} "
                        f"[reason={exc.reason_code}] Re-run approve to retry."
                    ),
                    "failed_step": "d",
                    "reason_code": exc.reason_code,
                }
        except Exception as exc:
            return {
                "ok": False,
                "error": f"Step d (create tasks): {exc}. Re-run approve to retry.",
                "failed_step": "d",
            }

        # Activate zero-dependency tasks in DB after creation.
        try:
            activated_tasks = _activate_tasks_db(wid, fid, actor)
        except Exception as exc:
            logger.warning("approve_feature: task activation (DB) failed: %s", exc)

    else:
        # Other stages/actions — DB update only.
        try:
            run_async(
                update_feature_stage(
                    workspace_id=wid,
                    feature_id=fid,
                    stage=stage,
                    review_status=stage_block["review_status"],
                    feature_status=new_feature_status,
                    current_stage=new_current_stage,
                    next_action=new_next_action,
                    actor=actor,
                    user_id=caller_user_id,
                    org_id=caller_org_id,
                )
            )
        except Exception as exc:
            logger.exception("approve_feature: DB update failed for feature %s", fid)
            return {"ok": False, "error": f"DB update failed: {exc}"}

    # Notify other workspace members. Only on a genuine new approval; the
    # already-approved fast-path above returns before reaching here.
    if action == "approve":
        try:
            run_async(notify_stage_approved(wid, fid, stage, caller_user_id, caller_org_id))
        except Exception:
            logger.exception("approve_feature: notify_stage_approved failed for feature %s", fid)

    result: Dict[str, Any] = {
        "ok": True,
        "feature_id": fid,
        "stage": stage,
        "action": action,
        "review_status": stage_block["review_status"],
        "feature_status": new_feature_status,
        "current_stage": new_current_stage,
        "commit_sha": commit_sha,
        "branch": None,
        "activated_tasks": activated_tasks,
    }
    if model_confirmation:
        result["model_confirmation"] = model_confirmation
    return result
