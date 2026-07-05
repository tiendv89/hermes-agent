"""Read-only access to the workflow-backend Postgres database.

Replaces HTTP calls to WORKFLOW_BACKEND_URL for workspace/feature data.
Requires WORKFLOW_DATABASE_URL pointing at the workflow-backend Postgres instance.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def check_workflow_available(**_: Any) -> bool:
    """Return True only when WORKFLOW_DATABASE_URL is configured."""
    return bool(os.environ.get("WORKFLOW_DATABASE_URL", "").strip())


def _validate_id(value: str, name: str) -> None:
    """Raise ValueError if value contains characters unsafe for URL path interpolation."""
    if not _ID_RE.match(value):
        raise ValueError(f"Invalid {name}: {value!r}")


def _conn() -> psycopg.Connection:
    url = os.environ.get("WORKFLOW_DATABASE_URL", "").strip()
    if not url:
        raise ValueError("WORKFLOW_DATABASE_URL is not set.")
    return psycopg.connect(url, row_factory=dict_row)


def get_workspace_context(workspace_id: str) -> Dict[str, Any]:
    """Return workspace metadata shaped for plugins tool consumers.

    Queries by slug first (the common case), falls back to UUID match.
    """
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT w.management_repo_id, s.repo_url
            FROM workspaces w
            LEFT JOIN workspace_github_sources s ON s.workspace_id = w.id
            WHERE w.slug = %s OR w.id::text = %s
            LIMIT 1
            """,
            (workspace_id, workspace_id),
        ).fetchone()

    if row is None:
        raise ValueError(f"Workspace not found: {workspace_id!r}")

    repos = []
    if row["repo_url"]:
        repos = [{"id": row["management_repo_id"], "github": row["repo_url"]}]

    return {
        "management_repo": row["management_repo_id"],
        "repos": repos,
    }


def get_workspace_organization_id(workspace_id: str) -> str | None:
    """Return the organization_id owning workspace_id, or None if not found."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT organization_id FROM workspaces WHERE slug = %s OR id::text = %s LIMIT 1",
            (workspace_id, workspace_id),
        ).fetchone()
    return row["organization_id"] if row else None


def get_workspace_slug(workspace_id: str) -> str:
    """Resolve a workspace identifier (slug or UUID) to its canonical slug.

    Callers that accept "slug or ID" (per the rest of this module) but must
    hand the value to a system that only understands the slug — e.g.
    GitNexus's connection-scoped endpoint — use this to normalize first.
    Returns "" when the workspace isn't found, so callers can fall back to
    the raw identifier instead of failing outright.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT w.slug FROM workspaces w WHERE w.slug = %s OR w.id::text = %s LIMIT 1",
            (workspace_id, workspace_id),
        ).fetchone()
    return row["slug"] if row else ""


def resolve_workspace_slug(workspace_id: str) -> str:
    """Best-effort normalize *workspace_id* (slug or UUID) to its canonical slug.

    Downstream systems like GitNexus and RAG key their per-workspace data by
    slug, not by this app's internal UUID — passing the UUID through would
    silently scope to a workspace they don't recognize. Falls back to the raw
    value when the workflow DB is unavailable or the lookup misses/errors, so
    callers degrade to passthrough instead of failing outright.
    """
    if not workspace_id or not check_workflow_available():
        return workspace_id
    try:
        return get_workspace_slug(workspace_id) or workspace_id
    except Exception:
        logger.debug(
            "resolve_workspace_slug: lookup failed for %r", workspace_id, exc_info=True
        )
        return workspace_id


def get_feature_detail(workspace_id: str, feature_id: str) -> Dict[str, Any]:
    """Return feature metadata and lifecycle state for the given workspace + feature."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT f.feature_name, f.title, f.current_stage, f.feature_status,
                   f.next_action, f.owner, f.init_pr_url
            FROM workspace_features f
            JOIN workspaces w ON w.id = f.workspace_id
            WHERE (w.slug = %s OR w.id::text = %s)
              AND (f.feature_name = %s OR f.feature_id::text = %s)
            LIMIT 1
            """,
            (workspace_id, workspace_id, feature_id, feature_id),
        ).fetchone()

    if row is None:
        raise ValueError(
            f"Feature {feature_id!r} not found in workspace {workspace_id!r}"
        )

    return {
        "feature_name": row["feature_name"],
        "title": row["title"],
        "stage": row["current_stage"],
        "status": row["feature_status"],
        "next_action": row["next_action"],
        "owner": row["owner"],
        "init_pr_url": row["init_pr_url"],
    }


def update_feature_stage(
    workspace_id: str,
    feature_id: str,
    stage: str,
    review_status: str,
    feature_status: str,
    current_stage: str,
    next_action: str,
    actor: str,
) -> None:
    """Persist stage-review state into workspace_features.stages JSONB (go-owner features).

    Merges the approval result into the stages column without touching fields
    owned by other stages. Also updates feature_status, current_stage, and
    next_action to advance the lifecycle.
    """
    import json as _json
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    with _conn() as conn:
        # Read current stages blob so we can merge.
        row = conn.execute(
            """
            SELECT f.id, f.stages
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

        stages: dict = row["stages"] if isinstance(row["stages"], dict) else {}
        stage_block = stages.setdefault(stage, {})
        stage_block["review_status"] = review_status
        stage_block["reviewed_by"] = actor
        stage_block["reviewed_at"] = now
        if "review_history" not in stage_block or not isinstance(stage_block["review_history"], list):
            stage_block["review_history"] = []
        stage_block["review_history"].append({
            "review_status": review_status,
            "reviewed_by": actor,
            "reviewed_at": now,
        })

        conn.execute(
            """
            UPDATE workspace_features
               SET stages        = %s::jsonb,
                   feature_status = %s,
                   current_stage  = %s,
                   next_action    = %s,
                   updated_at     = NOW()
             WHERE id = %s
            """,
            (
                _json.dumps(stages),
                feature_status,
                current_stage,
                next_action,
                row["id"],
            ),
        )


def get_feature_tasks(workspace_id: str, feature_id: str) -> list[dict]:
    """Return all tasks for the given workspace + feature, ordered by task_name."""
    with _conn() as conn:
        rows = conn.execute(
            """
            SELECT t.task_name, t.title, t.status, t.blocked_reason,
                   t.depends_on, t.pr, t.execution
            FROM workspace_tasks t
            JOIN workspace_features f ON f.id = t.feature_id
            JOIN workspaces w ON w.id = f.workspace_id
            WHERE (w.slug = %s OR w.id::text = %s)
              AND (f.feature_name = %s OR f.feature_id::text = %s)
            ORDER BY t.task_name
            """,
            (workspace_id, workspace_id, feature_id, feature_id),
        ).fetchall()
    return [dict(r) for r in rows]
