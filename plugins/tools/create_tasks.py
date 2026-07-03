"""create_tasks tool — backup step-d: create tasks via workflow-backend API.

Hermes-internal backup for the tasks-stage approve pipeline. Does ONLY step d
(create tasks) — never a (promote git), b (ensure docs on base), or c (DB
status update). Use when steps a, b, c have already succeeded and task
creation needs a manual trigger (e.g. after a partial failure on step d).

Guard error relay (server-side guard from T3 / workflow-backend):
  feature_not_tasks_approved → re-run the approve command to complete a→b→c first
  tasks_already_exist         → safe no-op ("tasks already exist")
  missing_config             → WORKFLOW_BACKEND_URL or service token not configured
  empty_tasks                → tasks.md Index table has no rows
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


def _relay_create_tasks_reason_code(reason_code: str) -> str:
    """Return a chat-facing guidance message for a workflow-backend reason code.

    Tailored for the backup /create-tasks context: any a/b/c gap points the
    user at the approve command rather than asking them to fix it manually.
    """
    if reason_code == "feature_not_tasks_approved":
        return (
            "The tasks stage is not yet approved or the docs PR has not been merged. "
            "Re-run the approve command to complete steps a→b→c, then retry /create-tasks."
        )
    if reason_code == "missing_config":
        return (
            "Missing configuration: WORKFLOW_BACKEND_URL or WORKFLOW_BACKEND_SERVICE_TOKEN "
            "is not set. Contact your platform team to provision these values."
        )
    if reason_code == "empty_tasks":
        return (
            "No tasks found in the tasks.md Index table. "
            "Verify that tasks.md has a valid Index table with at least one task row."
        )
    return "An unexpected error occurred when creating tasks in workflow-backend."


SCHEMA: Dict[str, Any] = {
    "description": (
        "Backup command: create tasks for the current feature via the workflow-backend "
        "API (step d only). Use this when the tasks-stage approve command completed "
        "steps a (promote), b (merge docs PR), and c (DB status) but failed at step d "
        "(task creation), or when a manual nudge is needed after those steps succeeded.\n\n"
        "This tool does ONLY task creation — it never promotes the feature, merges the "
        "docs PR, or updates the DB feature status. The workflow-backend guard enforces "
        "preconditions server-side.\n\n"
        "Guard error relay:\n"
        "- feature_not_tasks_approved → re-run the approve command to complete a→b→c first\n"
        "- tasks_already_exist → tasks already exist — nothing to do (safe no-op)\n\n"
        "Omit workspace_id/feature_id to use the current session context."
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


def handle(
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> Dict[str, Any]:
    from ..context import get_feature_id, get_workspace_id
    from ..db import get_feature_detail, get_workspace_context
    from ..document_repo import read_document
    from .approve import _resolve_status_branch_and_path, _run_async_create_tasks
    from .artifacts import _resolve_management_repo
    from src.services.workflow_backend_client import WorkflowBackendError

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()

    if not wid or not fid:
        return {
            "ok": False,
            "error": (
                "workspace_id and feature_id are required but were not provided and "
                "no context is set. Ensure you are in an active feature session."
            ),
        }

    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    try:
        workspace_context = get_workspace_context(wid)
        gh_owner, gh_repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        return {"ok": False, "error": f"Could not resolve management repo: {exc}"}

    base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")

    feature_name: Optional[str] = None
    init_pr_url: Optional[str] = None
    try:
        detail = get_feature_detail(wid, fid)
        feature_name = detail.get("feature_name")
        init_pr_url = detail.get("init_pr_url")
    except Exception as exc:
        logger.debug("create_tasks: could not fetch feature_detail: %s", exc)

    branch, _status_path = _resolve_status_branch_and_path(
        gh_owner, gh_repo, fid, feature_name, init_pr_url, base_branch, github_token
    )

    doc_dir = feature_name or fid
    try:
        tasks_md_result = read_document(
            gh_owner, gh_repo, branch, f"docs/features/{doc_dir}/tasks.md", github_token
        )
        tasks_md_content = tasks_md_result.get("content", "")
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Could not read tasks.md from branch {branch!r}: {exc}",
        }

    if not tasks_md_content:
        return {
            "ok": False,
            "error": (
                f"tasks.md not found on branch {branch!r} at "
                f"docs/features/{doc_dir}/tasks.md. "
                "Ensure the feature has a valid tasks.md."
            ),
        }

    try:
        result = _run_async_create_tasks(wid, fid, tasks_md_content)
    except WorkflowBackendError as exc:
        if exc.reason_code == "tasks_already_exist":
            logger.info(
                "create_tasks: tasks already exist for feature %s/%s — no-op",
                wid,
                fid,
            )
            return {
                "ok": True,
                "noop": True,
                "message": "Tasks already exist for this feature — nothing to do.",
            }
        relay_msg = _relay_create_tasks_reason_code(exc.reason_code)
        return {
            "ok": False,
            "error": relay_msg,
            "reason_code": exc.reason_code,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Task creation failed: {exc}",
        }

    return {
        "ok": True,
        "message": "Tasks created successfully.",
        "result": result,
    }
