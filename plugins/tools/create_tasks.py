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
from typing import Any

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
            "No tasks found in the tasks.md Index table. Verify that tasks.md has a "
            "valid Index table with columns | ID | Title | Repo | Depends On | Actor | "
            "and at least one T<n> row."
        )
    return "An unexpected error occurred when creating tasks in workflow-backend."


SCHEMA: dict[str, Any] = {
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


def load_feature_tasks_md(
    workspace_id: str,
    feature_id: str,
) -> dict[str, Any]:
    """Read the current feature's tasks.md from storage-service.

    Shared by the backup /create-tasks tool and the parse_tasks tool so both
    read the document the same way.

    Returns ``{"ok": True, "tasks_md": <content>}`` or
    ``{"ok": False, "error": <message>}``.
    """
    from plugins.clients.storage_service_client import read_document_content

    from ..context import get_org_id, get_user_id

    caller_user_id = get_user_id()
    caller_org_id = get_org_id()

    try:
        result = read_document_content(
            workspace_id, feature_id, "tasks.md",
            user_id=caller_user_id, org_id=caller_org_id,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": f"Could not read tasks.md from storage-service: {exc}",
        }
    content = result.get("content", "")
    if not content:
        return {
            "ok": False,
            "error": f"tasks.md not found in storage-service for feature {feature_id!r}.",
        }
    return {"ok": True, "tasks_md": content}


def handle(
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    from ..context import get_feature_id, get_workspace_id

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

    from src.services.workflow_backend_client import WorkflowBackendError

    from .approve import _run_async_create_tasks
    from .parse_tasks import parse_tasks_index

    loaded = load_feature_tasks_md(wid, fid)
    if not loaded.get("ok"):
        return {"ok": False, "error": loaded.get("error", "Could not read tasks.md.")}

    tasks = parse_tasks_index(loaded["tasks_md"])
    if not tasks:
        return {
            "ok": False,
            "error": (
                "No tasks parsed from tasks.md. It must contain an Index table with "
                "columns | ID | Title | Repo | Depends On | Actor | Model | and at least one "
                "T<n> row."
            ),
        }

    # Resolve display-name model fields to model_id UUIDs before creating tasks.
    # This is the same step run in approve.py step d — the backup /create-tasks
    # tool must also re-confirm before every retry (product spec Goal 4).
    from ..context import get_org_id, get_user_id
    from .model_resolution import format_unresolved_error, resolve_task_models

    resolution = resolve_task_models(
        wid, tasks, user_id=get_user_id() or "", org_id=get_org_id() or ""
    )
    if not resolution["ok"]:
        return {
            "ok": False,
            "error": format_unresolved_error(resolution["unresolved"]),
            "unresolved_models": resolution["unresolved"],
        }
    resolved_tasks = resolution["tasks"]

    try:
        result = _run_async_create_tasks(wid, fid, resolved_tasks)
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
