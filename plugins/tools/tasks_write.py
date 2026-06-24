"""write_tasks tool — generate task breakdown for a feature.

For ts/git features:
  - Commits tasks.md (narrative) + tasks/T{n}.yaml (machine state) to the
    init branch (or feature branch if init is merged).

For go/postgres features:
  - Commits tasks.md (narrative only) to the init branch.
  - Inserts task rows directly into workspace_tasks (DB is the source of truth).

Task YAML schema matches agent-workflow's canonical format:
  id, title, repo, status, depends_on, blocked_reason, branch,
  execution.{actor_type, last_updated_by, last_updated_at},
  pr.{url, status}, log[]
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import yaml

from ..db import _validate_id, get_feature_detail, get_workspace_context
from ..document_repo import branch_exists
from .artifacts import _resolve_management_repo

logger = logging.getLogger(__name__)

_TASK_ID_RE = __import__("re").compile(r"^T\d+$")

SCHEMA: Dict[str, Any] = {
    "description": (
        "Generate the task breakdown for a feature and commit it. "
        "For ts/git features: commits tasks.md narrative + one tasks/T{n}.yaml per task "
        "to the feature branch. "
        "For go/postgres features: commits tasks.md only and inserts tasks into the DB. "
        "REQUIRED FIRST: call read_document(document='technical_design') (and 'product_spec') to "
        "load the approved design from the feature branch and derive the task list from its actual "
        "content — never infer tasks from RAG or the request text. "
        "Each task's 'repo' MUST be a real repo name from query_gitnexus(tool='list_repos'); "
        "determine it by querying GitNexus for the symbols/files the task touches and using the "
        "repo that contains them — do NOT guess the repo from the feature title or use workspace.yaml. "
        "Call this after technical_design is approved and you have designed the full task list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": (
                    "Ordered list of tasks. Each task must have an id (T1, T2, ...), "
                    "title, and optionally: repo, depends_on (list of task IDs), "
                    "actor_type ('agent' | 'human' | 'either')."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID, e.g. T1"},
                        "title": {"type": "string"},
                        "repo": {"type": "string", "description": "Repo slug this task targets."},
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Task IDs this task depends on. Empty means it can start immediately.",
                        },
                        "actor_type": {
                            "type": "string",
                            "enum": ["agent", "human", "either"],
                            "description": "Who executes this task. Defaults to 'agent'.",
                        },
                    },
                    "required": ["id", "title"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "tasks_md": {
                "type": "string",
                "description": (
                    "Full narrative tasks.md content — dependency diagram, index table, "
                    "and per-task sections (## T{n} — {Title} with Description, "
                    "Required skills, Subtasks). This is the human-readable breakdown."
                ),
            },
            "commit_message": {
                "type": "string",
                "description": "Git commit message (optional).",
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
        "required": ["tasks", "tasks_md"],
        "additionalProperties": False,
    },
}


def _task_yaml(task: Dict[str, Any], feature_slug: str) -> str:
    """Render a task dict into canonical agent-workflow YAML."""
    tid = task["id"]
    doc = {
        "id": tid,
        "title": task.get("title", ""),
        "repo": task.get("repo") or "",
        "status": "todo",
        "depends_on": task.get("depends_on") or [],
        "blocked_reason": None,
        "blocked_suggestion": None,
        "blocked_details": None,
        "blocked_context": None,
        "branch": f"feature/{feature_slug}",
        "execution": {
            "actor_type": task.get("actor_type") or "agent",
            "last_updated_by": None,
            "last_updated_at": None,
            "suggested_next_step": None,
            "requires_human_review": False,
        },
        "execution_handle": None,
        "pr": {
            "url": "",
            "status": "not_created",
        },
        "workspace_pr": None,
        "log": [],
    }
    return yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _resolve_target_branch(
    gh_owner: str,
    gh_repo: str,
    feature_id: str,
    feature_name: str,
    init_pr_url: Optional[str],
    github_token: str,
) -> tuple[str, str]:
    """Return (branch, doc_dir) — the branch to commit to and the feature doc directory.

    All git artifacts are slug-keyed and the init branch (feature/{slug}-init) is
    the canonical design-phase branch. Prefer it whenever it exists, independent
    of init_pr_url; fall back to feature/{slug} only when there is no init branch.
    Docs always live under docs/features/{slug}/.
    """
    slug = feature_name or feature_id
    init_branch = f"feature/{slug}-init"
    if branch_exists(gh_owner, gh_repo, init_branch, github_token):
        return init_branch, slug
    return f"feature/{slug}", slug


def _commit_files(
    gh_owner: str,
    gh_repo: str,
    branch: str,
    files: Dict[str, str],
    commit_msg: str,
    github_token: str,
) -> str:
    """Commit multiple files to a branch in a single commit using the Git Data API.

    Uses requests (same as document_repo.py) so the certifi CA bundle is used
    for SSL verification, matching the rest of the hermes-agent GitHub client code.
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

    def _get(url: str) -> Any:
        r = _requests.get(url, headers=headers, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _post(url: str, body: Any) -> Any:
        r = _requests.post(url, headers=headers, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _patch(url: str, body: Any) -> Any:
        r = _requests.patch(url, headers=headers, json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()

    # Get latest commit SHA on branch
    ref = _get(f"{api}/repos/{gh_owner}/{gh_repo}/git/refs/heads/{branch}")
    base_sha = ref["object"]["sha"]

    # Get base tree SHA
    commit_obj = _get(f"{api}/repos/{gh_owner}/{gh_repo}/git/commits/{base_sha}")
    base_tree = commit_obj["tree"]["sha"]

    # Create blobs for each file
    tree_entries = []
    for path, content in files.items():
        blob = _post(f"{api}/repos/{gh_owner}/{gh_repo}/git/blobs", {
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "encoding": "base64",
        })
        tree_entries.append({"path": path.lstrip("/"), "mode": "100644", "type": "blob", "sha": blob["sha"]})

    # Create tree on top of base
    tree = _post(f"{api}/repos/{gh_owner}/{gh_repo}/git/trees", {
        "base_tree": base_tree,
        "tree": tree_entries,
    })

    # Create commit
    commit = _post(f"{api}/repos/{gh_owner}/{gh_repo}/git/commits", {
        "message": commit_msg,
        "tree": tree["sha"],
        "parents": [base_sha],
    })

    # Advance branch ref
    _patch(f"{api}/repos/{gh_owner}/{gh_repo}/git/refs/heads/{branch}", {
        "sha": commit["sha"],
        "force": False,
    })

    return commit["sha"]


def _insert_tasks_to_db(
    workspace_id: str,
    feature_id: str,
    tasks: List[Dict[str, Any]],
) -> None:
    """Upsert task rows into workspace_tasks for go/postgres features.

    task_id is a UUID PK; we match on (feature_id, task_name) to detect
    existing rows and update rather than insert duplicates.
    """
    import json as _json
    import uuid as _uuid
    from ..db import _conn

    with _conn() as conn:
        # Look up the feature's DB PKs
        row = conn.execute(
            """
            SELECT f.id AS feature_pk, f.feature_name, w.id AS workspace_pk
            FROM workspace_features f
            JOIN workspaces w ON w.id = f.workspace_id
            WHERE (w.slug = %s OR w.id::text = %s)
              AND (f.feature_name = %s OR f.feature_id::text = %s)
            LIMIT 1
            """,
            (workspace_id, workspace_id, feature_id, feature_id),
        ).fetchone()

        if row is None:
            raise ValueError(f"Feature {feature_id!r} not found in workspace {workspace_id!r}")

        feature_pk = row["feature_pk"]
        feature_name = row["feature_name"]
        workspace_pk = row["workspace_pk"]

        for task in tasks:
            task_name = task["id"]  # e.g. "T1"
            actor_type = task.get("actor_type") or "agent"
            depends_on = _json.dumps(task.get("depends_on") or [])
            execution = _json.dumps({
                "actor_type": actor_type,
                "last_updated_by": None,
                "last_updated_at": None,
            })
            pr_val = _json.dumps({"url": "", "status": "not_created"})
            repo = task.get("repo") or None

            # Check if this task_name already exists for this feature.
            existing = conn.execute(
                "SELECT id FROM workspace_tasks WHERE feature_id = %s AND task_name = %s LIMIT 1",
                (feature_pk, task_name),
            ).fetchone()

            if existing:
                conn.execute(
                    """
                    UPDATE workspace_tasks
                       SET title      = %s,
                           depends_on = %s::jsonb,
                           repo       = %s,
                           execution  = %s::jsonb,
                           owner      = 'go',
                           updated_at = NOW()
                     WHERE id = %s
                    """,
                    (task.get("title", ""), depends_on, repo, execution, existing["id"]),
                )
            else:
                new_task_id = str(_uuid.uuid4())
                conn.execute(
                    """
                    INSERT INTO workspace_tasks
                        (workspace_id, feature_id, feature_name, task_id, task_name,
                         title, status, depends_on, repo, execution, pr, owner)
                    VALUES (%s, %s, %s, %s::uuid, %s, %s, 'todo',
                            %s::jsonb, %s, %s::jsonb, %s::jsonb, 'go')
                    """,
                    (
                        workspace_pk, feature_pk, feature_name, new_task_id,
                        task_name, task.get("title", ""),
                        depends_on, repo, execution, pr_val,
                    ),
                )


def handle(
    tasks: List[Dict[str, Any]],
    tasks_md: str,
    commit_message: str = "",
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> Dict[str, Any]:
    from ..context import get_feature_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()

    if not wid or not fid:
        return {"ok": False, "error": "workspace_id and feature_id are required but were not provided and no context is set."}

    if not tasks:
        return {"ok": False, "error": "tasks must be a non-empty list."}

    if not tasks_md or not tasks_md.strip():
        return {"ok": False, "error": "tasks_md (narrative markdown) is required."}

    # Validate task IDs
    for t in tasks:
        tid = t.get("id", "")
        if not _TASK_ID_RE.match(tid):
            return {"ok": False, "error": f"Invalid task id {tid!r}. Must match T<number>, e.g. T1, T2."}

    # Validate each task's repo against GitNexus's indexed repo set — the
    # authoritative repo universe. This is the guardrail behind the "determine
    # repo from GitNexus, not guesswork" rule: reject tasks pointed at a repo
    # that isn't actually indexed. Skipped gracefully when GitNexus is
    # unavailable (list_indexed_repos returns None) so authoring still works.
    from .gitnexus import list_indexed_repos

    indexed_repos = list_indexed_repos()
    if indexed_repos:
        indexed_set = set(indexed_repos)
        unknown = sorted({(t.get("repo") or "").strip() for t in tasks if (t.get("repo") or "").strip()} - indexed_set)
        if unknown:
            return {
                "ok": False,
                "error": (
                    f"Task repo(s) not indexed in GitNexus: {unknown}. "
                    f"Valid repos: {sorted(indexed_set)}. "
                    "Set each task's repo to the GitNexus repo that actually contains the code it "
                    "touches — call query_gitnexus(tool='list_repos') and query the relevant symbols "
                    "to confirm. Do not guess the repo from the feature title."
                ),
            }

    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    try:
        _validate_id(fid, "feature_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # Feature detail
    feature_name: Optional[str] = None
    init_pr_url: Optional[str] = None
    owner: Optional[str] = None
    try:
        detail = get_feature_detail(wid, fid)
        feature_name = detail.get("feature_name") or fid
        init_pr_url = detail.get("init_pr_url")
        owner = detail.get("owner") or "ts"
    except Exception as exc:
        logger.debug("write_tasks: could not fetch feature_detail: %s", exc)
        feature_name = fid
        owner = "ts"

    try:
        workspace_context = get_workspace_context(wid)
        gh_owner, gh_repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        return {"ok": False, "error": f"Could not resolve management repo: {exc}"}

    branch, doc_dir = _resolve_target_branch(
        gh_owner, gh_repo, fid, feature_name, init_pr_url, github_token
    )

    # Build files to commit
    files: Dict[str, str] = {
        f"docs/features/{doc_dir}/tasks.md": tasks_md,
    }

    if owner != "go":
        # ts/git: also write per-task YAML files
        for task in tasks:
            tid = task["id"]
            yaml_content = _task_yaml(task, feature_name or fid)
            files[f"docs/features/{doc_dir}/tasks/{tid}.yaml"] = yaml_content

    commit_msg = commit_message or f"feat({feature_name}): add task breakdown ({len(tasks)} tasks)"

    try:
        commit_sha = _commit_files(gh_owner, gh_repo, branch, files, commit_msg, github_token)
    except Exception as exc:
        logger.exception("write_tasks: commit failed for feature %s", fid)
        return {"ok": False, "error": f"Git commit failed: {exc}"}

    # go/postgres: insert tasks into DB
    if owner == "go":
        try:
            _insert_tasks_to_db(wid, fid, tasks)
        except Exception as exc:
            logger.exception("write_tasks: DB insert failed for feature %s", fid)
            return {
                "ok": False,
                "error": f"tasks.md committed (sha={commit_sha}) but DB insert failed: {exc}",
            }

    return {
        "ok": True,
        "owner": owner,
        "branch": branch,
        "commit_sha": commit_sha,
        "tasks_committed": len(tasks),
        "files_written": list(files.keys()),
        "db_tasks_inserted": len(tasks) if owner == "go" else 0,
        "message": (
            f"Task breakdown written: {len(tasks)} tasks committed to {branch}. "
            + ("Task YAML files written in tasks/. " if owner != "go" else "Task state stored in DB. ")
            + "Call approve_feature(stage='tasks') when ready to activate tasks."
        ),
    }
