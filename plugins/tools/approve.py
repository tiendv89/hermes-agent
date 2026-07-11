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

from ..document_repo import StaleBaseError, commit_to_branch, read_document
from ..storage_service_client import read_document_content
from ..validation import _validate_id
from .artifacts import _resolve_management_repo
from src.services.approval_notifications import notify_stage_approved
from src.services.workflow_backend_client import (
    activate_ready_tasks,
    get_feature_detail,
    get_workspace_context,
    run_async,
    update_feature_stage,
)

_TASK_FILE_RE = _re.compile(r"^docs/features/[^/]+/tasks/(T\d+)\.yaml$")

logger = logging.getLogger(__name__)


def _find_open_prs(
    gh_owner: str,
    gh_repo: str,
    head_branch: str,
    base_branch: str,
    github_token: str,
) -> list:
    """Return list of open PRs matching head_branch → base_branch."""
    import requests as _requests

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/pulls"
    params = {
        "state": "open",
        "head": f"{gh_owner}:{head_branch}",
        "base": base_branch,
        "per_page": 10,
    }
    r = _requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def _merge_pr(
    gh_owner: str,
    gh_repo: str,
    pr_number: int,
    github_token: str,
) -> None:
    """Merge a PR via the GitHub Merges API."""
    import requests as _requests

    headers = {
        "Authorization": f"Bearer {github_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{gh_owner}/{gh_repo}/pulls/{pr_number}/merge"
    r = _requests.put(url, headers=headers, json={"merge_method": "squash"}, timeout=30)
    if r.status_code == 405:
        raise RuntimeError(
            f"PR #{pr_number} is not mergeable (possibly has conflicts or is already merged): {r.text[:200]}"
        )
    if r.status_code == 409:
        raise RuntimeError(f"PR #{pr_number} merge conflict: {r.text[:200]}")
    r.raise_for_status()


def _read_status_yaml_on_branch(
    gh_owner: str,
    gh_repo: str,
    branch: str,
    path: str,
    github_token: str,
) -> Optional[dict]:
    """Read and parse status.yaml from a branch; return None if not found or parse error."""
    try:
        result = read_document(gh_owner, gh_repo, branch, path, github_token)
        if not result["content"]:
            return None
        return yaml.safe_load(result["content"])
    except Exception:
        return None


def _ensure_docs_on_base(
    gh_owner: str,
    gh_repo: str,
    docs_branch: str,
    base_branch: str,
    status_path: str,
    github_token: str,
) -> Optional[str]:
    """Step b: ensure the feature docs are on base_branch (content-keyed check).

    Returns None on success (already on base, or PR merged successfully).
    Returns a human-readable error string that should be returned to chat on halt.
    """
    # Check if the base branch already has the approved docs.
    base_status = _read_status_yaml_on_branch(
        gh_owner, gh_repo, base_branch, status_path, github_token
    )
    if base_status is not None:
        base_tasks = base_status.get("stages", {}).get("tasks", {})
        if base_tasks.get("review_status") == "approved":
            logger.info(
                "ensure_docs_on_base: base branch %r already contains approved docs — skip",
                base_branch,
            )
            return None

    # Docs not yet on base — find open PRs from docs_branch → base_branch.
    try:
        open_prs = _find_open_prs(
            gh_owner, gh_repo, docs_branch, base_branch, github_token
        )
    except Exception as exc:
        return (
            f"Could not list open PRs from '{docs_branch}' to '{base_branch}': {exc}. "
            "Re-run approve after verifying GitHub access."
        )

    if len(open_prs) == 1:
        pr = open_prs[0]
        pr_number = pr["number"]
        pr_url = pr.get("html_url", f"PR #{pr_number}")
        try:
            _merge_pr(gh_owner, gh_repo, pr_number, github_token)
            logger.info("ensure_docs_on_base: merged PR #%d (%s)", pr_number, pr_url)
            return None
        except Exception as exc:
            return (
                f"Merging docs PR #{pr_number} ({pr_url}) from '{docs_branch}' to "
                f"'{base_branch}' failed: {exc}. "
                "Resolve the conflict or merge the PR manually, then re-run approve."
            )
    elif len(open_prs) == 0:
        return (
            f"No open PR found from '{docs_branch}' to '{base_branch}', and the docs "
            f"are not yet on the base branch. "
            f"Open a PR from '{docs_branch}' to '{base_branch}' and re-run approve."
        )
    else:
        pr_refs = ", ".join(
            f"#{pr['number']} ({pr.get('html_url', '')})" for pr in open_prs
        )
        return (
            f"Multiple open PRs match '{docs_branch}' → '{base_branch}': {pr_refs}. "
            "Close all but one and re-run approve."
        )


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

    done_ids = {
        tid for tid, t in tasks_by_id.items() if (t.get("status") or "") == "done"
    }
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
        t["log"].append(
            {
                "action": "ready",
                "by": actor,
                "at": now,
                "note": "Task activated on tasks-stage approval — dependencies met.",
            }
        )
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
            gh_owner,
            gh_repo,
            branch,
            updated_files,
            f"chore: activate {len(activated)} task(s) on tasks approval",
            github_token,
        )
        return {"activated": activated, "commit_sha": commit_sha}
    except Exception as exc:
        logger.warning("activate_tasks: commit failed: %s", exc)
        return {"activated": activated, "commit_sha": ""}


def _activate_tasks_db(workspace_id: str, feature_id: str, actor: str) -> list:
    """Set zero-dependency (or now-unblocked) tasks to ready in workspace_tasks (go features)."""
    from ..context import get_org_id, get_user_id

    return run_async(
        activate_ready_tasks(workspace_id, feature_id, user_id=get_user_id(), org_id=get_org_id())
    )


def _now_utc() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S+0000"
    )


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

    All git artifacts are slug-keyed and the init branch (feature/{slug}-init)
    is the canonical design-phase branch. Prefer it whenever it exists,
    independent of init_pr_url; fall back to feature/{slug} only when there is
    no init branch.
    """
    slug = feature_name or feature_id
    init_branch = f"feature/{slug}-init"
    path = f"docs/features/{slug}/status.yaml"

    from ..document_repo import branch_exists

    if branch_exists(gh_owner, gh_repo, init_branch, github_token):
        return init_branch, path

    return f"feature/{slug}", path


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

    # Look up feature name, owner, and init PR URL first so we can branch on
    # owner before requiring GITHUB_TOKEN or resolving the management repo —
    # go-owned features need neither (workflow-backend no longer creates an
    # init branch/PR at feature creation for go, see feature_create.go's early
    # return for owner == "go").
    feature_name: Optional[str] = None
    init_pr_url: Optional[str] = None
    owner: Optional[str] = None
    detail: Dict[str, Any] = {}
    try:
        detail = run_async(get_feature_detail(wid, fid, user_id=caller_user_id, org_id=caller_org_id))
        feature_name = detail.get("feature_name")
        init_pr_url = detail.get("init_pr_url")
        owner = detail.get("owner")
    except Exception as exc:
        logger.debug("approve_feature: could not fetch feature_detail: %s", exc)

    branch: Optional[str] = None
    path: Optional[str] = None
    current: Dict[str, Any] = {"sha": None}
    status_data: dict
    github_token: str = ""
    gh_owner: str = ""
    gh_repo: str = ""

    if owner == "go":
        # go-owned features track status entirely in workflow-backend's DB —
        # there is no git branch/status.yaml for them.
        status_data = {
            "feature_status": detail.get("status") or "",
            "current_stage": detail.get("stage") or "",
            "next_action": detail.get("next_action") or "",
            "stages": dict(detail.get("stages") or {}),
        }
    else:
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()
        if not github_token:
            return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

        try:
            workspace_context = run_async(get_workspace_context(wid, user_id=caller_user_id, org_id=caller_org_id))
            gh_owner, gh_repo = _resolve_management_repo(workspace_context)
        except Exception as exc:
            return {"ok": False, "error": f"Could not resolve management repo: {exc}"}

        base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")

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
            status_data = yaml.safe_load(current["content"])
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
    # Exception: go-tasks-approve falls through so steps b/c/d can run (resumable pipeline).
    already_approved = (
        stage_block.get("review_status") == "approved" and action == "approve"
    )
    _is_go_tasks_approve = owner == "go" and stage == "tasks" and action == "approve"
    if already_approved and not _is_go_tasks_approve:
        activated_tasks: list = []
        if stage == "tasks":
            doc_dir = feature_name or fid
            if owner == "go":
                try:
                    activated_tasks = _activate_tasks_db(wid, fid, actor)
                except Exception as exc:
                    logger.warning(
                        "approve_feature (already-approved fast-path): DB activation failed: %s",
                        exc,
                    )
            else:
                activation = _activate_tasks_git(
                    gh_owner, gh_repo, branch, doc_dir, actor, github_token
                )
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
                + (
                    f"Activated {len(activated_tasks)} task(s): {activated_tasks}."
                    if activated_tasks
                    else "No additional tasks to activate (all are already ready/in-progress/done)."
                )
            ),
        }
    # If already_approved and _is_go_tasks_approve: fall through with step_a already done.

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
        commit_msg = f"chore: approve {stage} stage"

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
        commit_msg = f"chore: reject {stage} stage"

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
        commit_msg = f"chore: reopen {stage} stage"

    new_feature_status = status_data.get("feature_status", "")
    new_current_stage = status_data.get("current_stage", "")
    new_next_action = status_data.get("next_action", "")

    commit_sha = ""
    activated_tasks: list = []
    doc_dir = feature_name or fid

    if _is_go_tasks_approve:
        # go-tasks-approve pipeline — status lives entirely in workflow-backend's
        # DB and docs live in storage-service, so this collapses to: DB status
        # update, then create/activate tasks. (Previously also committed
        # status.yaml to git and merged the init PR — both removed now that
        # workflow-backend no longer creates a git branch/PR for go features.)

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
                    "| ID | Title | Repo | Depends On | Actor | and at least one "
                    "T<n> row. Fix tasks.md and re-run approve to retry."
                ),
                "failed_step": "d",
            }

        from src.services.workflow_backend_client import WorkflowBackendError as _WBE

        try:
            _run_async_create_tasks(wid, fid, tasks)
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

    elif owner == "go":
        # go/postgres features: other stages/actions — DB update only.
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

    else:
        # ts/git features: commit updated status.yaml to the feature branch or init branch.
        new_content = yaml.dump(
            status_data, default_flow_style=False, allow_unicode=True
        )
        try:
            if branch.endswith("-init"):
                commit_sha = commit_to_branch(
                    gh_owner,
                    gh_repo,
                    branch,
                    path,
                    new_content,
                    current["sha"],
                    commit_msg,
                    github_token,
                )
            else:
                from ..document_repo import write_document

                # Pass the slug so write_document commits to feature/{slug}.
                result = write_document(
                    gh_owner,
                    gh_repo,
                    (feature_name or fid),
                    base_branch,
                    path,
                    new_content,
                    current["sha"],
                    commit_msg,
                    github_token,
                )
                commit_sha = result.get("commit_sha", "")
        except StaleBaseError as exc:
            return {
                "ok": False,
                "conflict": True,
                "error": f"status.yaml changed since it was read. Retry. ({exc})",
            }
        except Exception as exc:
            logger.exception("approve_feature: commit failed for feature %s", fid)
            return {"ok": False, "error": str(exc)}

        # Activate zero-dependency tasks when approving the tasks stage.
        if stage == "tasks" and action == "approve":
            activation = _activate_tasks_git(
                gh_owner, gh_repo, branch, doc_dir, actor, github_token
            )
            activated_tasks = activation.get("activated", [])
            if activation.get("commit_sha"):
                commit_sha = activation["commit_sha"]

    # Notify other workspace members (mirrors the human-facing
    # POST /features/{id}/stage-transition endpoint's own notify_stage_approved
    # call in src/api/routers/stages.py — this agent-tool path previously had
    # no notification wiring at all). Only on a genuine new approval; the
    # already-approved fast-path above returns before reaching here.
    if action == "approve":
        try:
            run_async(notify_stage_approved(wid, fid, stage, caller_user_id, caller_org_id))
        except Exception:
            logger.exception("approve_feature: notify_stage_approved failed for feature %s", fid)

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
