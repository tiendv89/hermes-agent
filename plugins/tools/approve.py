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

import re as _re

from ..db import _validate_id, get_feature_detail, get_workspace_context, update_feature_stage
from ..document_repo import StaleBaseError, branch_exists, commit_to_branch, read_document
from .artifacts import _resolve_management_repo

_TASK_FILE_RE = _re.compile(r"^docs/features/[^/]+/tasks/(T\d+)\.yaml$")

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


def _activate_tasks_git(
    gh_owner: str,
    gh_repo: str,
    branch: str,
    doc_dir: str,
    actor: str,
    github_token: str,
) -> Dict[str, Any]:
    """Read all tasks/T{n}.yaml on branch, set zero-dependency ones to ready, commit back.

    Returns {"activated": [task_ids], "commit_sha": sha} or {"activated": [], "commit_sha": ""}
    on any read/write error (logged, not fatal — the stage approval already succeeded).
    """
    import base64
    import json
    import requests as _requests

    api = "https://api.github.com"
    timeout = 30
    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    # List all files in the tasks/ directory
    tasks_path = f"docs/features/{doc_dir}/tasks"
    r = _requests.get(
        f"{api}/repos/{gh_owner}/{gh_repo}/contents/{tasks_path}",
        headers=headers,
        params={"ref": branch},
        timeout=timeout,
    )
    if r.status_code == 404:
        return {"activated": [], "commit_sha": ""}
    if not r.ok:
        logger.warning("activate_tasks: listing %s failed: %s", tasks_path, r.text)
        return {"activated": [], "commit_sha": ""}

    entries = [e for e in r.json() if e.get("name", "").endswith(".yaml")]
    if not entries:
        return {"activated": [], "commit_sha": ""}

    # Read all task YAMLs
    tasks_by_id: Dict[str, dict] = {}
    file_shas: Dict[str, str] = {}
    for entry in entries:
        m = _TASK_FILE_RE.match(entry["path"])
        if not m:
            continue
        task_id = m.group(1)
        fr = _requests.get(
            f"{api}/repos/{gh_owner}/{gh_repo}/contents/{entry['path']}",
            headers=headers,
            params={"ref": branch},
            timeout=timeout,
        )
        if not fr.ok:
            continue
        raw = base64.b64decode(fr.json()["content"].replace("\n", "")).decode("utf-8")
        try:
            t = yaml.safe_load(raw)
        except Exception:
            continue
        tasks_by_id[task_id] = t
        file_shas[task_id] = fr.json()["sha"]

    if not tasks_by_id:
        return {"activated": [], "commit_sha": ""}

    done_ids = {tid for tid, t in tasks_by_id.items() if (t.get("status") or "") == "done"}
    now = _now_utc()
    activated = []
    updated_files: Dict[str, str] = {}

    for task_id, t in tasks_by_id.items():
        if (t.get("status") or "todo") != "todo":
            continue  # only activate todo tasks

        deps = t.get("depends_on") or []
        if isinstance(deps, str):
            deps = [d.strip() for d in deps.split(",") if d.strip()]

        can_start = not deps or all(d in done_ids for d in deps)
        if not can_start:
            continue

        t["status"] = "ready"
        if not isinstance(t.get("log"), list):
            t["log"] = []
        t["log"].append({
            "action": "ready",
            "by": actor,
            "at": now,
            "note": "Task activated on tasks-stage approval — dependencies met.",
        })
        updated_files[f"docs/features/{doc_dir}/tasks/{task_id}.yaml"] = yaml.dump(
            t, default_flow_style=False, allow_unicode=True, sort_keys=False
        )
        activated.append(task_id)

    if not updated_files:
        return {"activated": [], "commit_sha": ""}

    # Commit all updated task files in one shot via Git Data API
    try:
        from .tasks_write import _commit_files
        commit_sha = _commit_files(
            gh_owner, gh_repo, branch, updated_files,
            f"chore: activate {len(activated)} task(s) on tasks approval",
            github_token,
        )
        return {"activated": activated, "commit_sha": commit_sha}
    except Exception as exc:
        logger.warning("activate_tasks: commit failed: %s", exc)
        return {"activated": activated, "commit_sha": ""}


def _activate_tasks_db(workspace_id: str, feature_id: str, actor: str) -> list:
    """Set zero-dependency tasks to ready in workspace_tasks (go features)."""
    from ..db import _conn

    activated = []
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT t.id, t.task_name, t.depends_on, t.status
            FROM workspace_tasks t
            JOIN workspace_features f ON f.id = t.feature_id
            JOIN workspaces w ON w.id = f.workspace_id
            WHERE (w.slug = %s OR w.id::text = %s)
              AND (f.feature_name = %s OR f.feature_id::text = %s)
              AND t.status = 'todo'
            """,
            (workspace_id, workspace_id, feature_id, feature_id),
        ).fetchall()

        # Collect done task names to evaluate dependencies
        done_rows = conn.execute(
            """
            SELECT t.task_name
            FROM workspace_tasks t
            JOIN workspace_features f ON f.id = t.feature_id
            JOIN workspaces w ON w.id = f.workspace_id
            WHERE (w.slug = %s OR w.id::text = %s)
              AND (f.feature_name = %s OR f.feature_id::text = %s)
              AND t.status = 'done'
            """,
            (workspace_id, workspace_id, feature_id, feature_id),
        ).fetchall()
        done_names = {r["task_name"] for r in done_rows}

        for row in rows:
            import json as _json
            deps = row["depends_on"]
            if isinstance(deps, str):
                try:
                    deps = _json.loads(deps)
                except Exception:
                    deps = []
            if not isinstance(deps, list):
                deps = []

            can_start = not deps or all(d in done_names for d in deps)
            if not can_start:
                continue

            conn.execute(
                "UPDATE workspace_tasks SET status = 'ready', updated_at = NOW() WHERE id = %s",
                (row["id"],),
            )
            activated.append(row["task_name"])

    return activated


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

    # Look up feature name, owner, and init PR URL.
    feature_name: Optional[str] = None
    init_pr_url: Optional[str] = None
    owner: Optional[str] = None
    try:
        detail = get_feature_detail(wid, fid)
        feature_name = detail.get("feature_name")
        init_pr_url = detail.get("init_pr_url")
        owner = detail.get("owner")
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

    # Fast-path: stage already approved — skip the status.yaml write but still
    # run task activation for stage=tasks so any unactivated tasks get set to ready.
    already_approved = stage_block.get("review_status") == "approved" and action == "approve"
    if already_approved:
        activated_tasks: list = []
        if stage == "tasks":
            doc_dir = feature_name or fid
            if owner == "go":
                try:
                    activated_tasks = _activate_tasks_db(wid, fid, actor)
                except Exception as exc:
                    logger.warning("approve_feature (already-approved fast-path): DB activation failed: %s", exc)
            else:
                activation = _activate_tasks_git(gh_owner, gh_repo, branch, doc_dir, actor, github_token)
                activated_tasks = activation.get("activated", [])
        return {
            "ok": True,
            "feature_id": fid,
            "stage": stage,
            "action": "noop",
            "owner": owner,
            "review_status": "approved",
            "feature_status": status_data.get("feature_status", ""),
            "current_stage": status_data.get("current_stage", ""),
            "commit_sha": "",
            "branch": branch if owner != "go" else None,
            "activated_tasks": activated_tasks,
            "note": (
                f"Stage '{stage}' was already approved — status.yaml unchanged. "
                + (f"Activated {len(activated_tasks)} task(s): {activated_tasks}." if activated_tasks
                   else "No additional tasks to activate (all are already ready/in-progress/done).")
            ),
        }

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

    new_feature_status = status_data.get("feature_status", "")
    new_current_stage = status_data.get("current_stage", "")
    new_next_action = status_data.get("next_action", "")

    commit_sha = ""
    activated_tasks: list = []

    if owner == "go":
        # go/postgres features: persist approval state directly in the DB.
        try:
            update_feature_stage(
                workspace_id=wid,
                feature_id=fid,
                stage=stage,
                review_status=stage_block["review_status"],
                feature_status=new_feature_status,
                current_stage=new_current_stage,
                next_action=new_next_action,
                actor=actor,
            )
        except Exception as exc:
            logger.exception("approve_feature: DB update failed for feature %s", fid)
            return {"ok": False, "error": f"DB update failed: {exc}"}

        # Activate zero-dependency tasks when approving the tasks stage.
        if stage == "tasks" and action == "approve":
            try:
                activated_tasks = _activate_tasks_db(wid, fid, actor)
            except Exception as exc:
                logger.warning("approve_feature: task activation (DB) failed: %s", exc)

    else:
        # ts/git features: commit updated status.yaml to the feature branch or init branch.
        new_content = yaml.dump(status_data, default_flow_style=False, allow_unicode=True)
        try:
            if branch.endswith("-init"):
                commit_sha = commit_to_branch(
                    gh_owner, gh_repo, branch, path, new_content,
                    current["sha"], commit_msg, github_token,
                )
            else:
                from ..document_repo import write_document
                result = write_document(
                    gh_owner, gh_repo, fid, base_branch, path, new_content,
                    current["sha"], commit_msg, github_token,
                )
                commit_sha = result.get("commit_sha", "")
        except StaleBaseError as exc:
            return {"ok": False, "conflict": True, "error": f"status.yaml changed since it was read. Retry. ({exc})"}
        except Exception as exc:
            logger.exception("approve_feature: commit failed for feature %s", fid)
            return {"ok": False, "error": str(exc)}

        # Activate zero-dependency tasks when approving the tasks stage.
        if stage == "tasks" and action == "approve":
            doc_dir = feature_name or fid
            activation = _activate_tasks_git(
                gh_owner, gh_repo, branch, doc_dir, actor, github_token
            )
            activated_tasks = activation.get("activated", [])
            if activation.get("commit_sha"):
                commit_sha = activation["commit_sha"]

    return {
        "ok": True,
        "feature_id": fid,
        "stage": stage,
        "action": action,
        "owner": owner,
        "review_status": stage_block["review_status"],
        "feature_status": new_feature_status,
        "current_stage": new_current_stage,
        "commit_sha": commit_sha,
        "branch": branch if owner != "go" else None,
        "activated_tasks": activated_tasks,
    }
