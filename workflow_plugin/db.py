"""Read-only access to the workflow-backend Postgres database.

Replaces HTTP calls to WORKFLOW_BACKEND_URL for workspace/feature data.
Requires WORKFLOW_DATABASE_URL pointing at the workflow-backend Postgres instance.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict

import psycopg
from psycopg.rows import dict_row

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
    """Return workspace metadata shaped for workflow_plugin tool consumers.

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


def get_feature_detail(workspace_id: str, feature_id: str) -> Dict[str, Any]:
    """Return feature lifecycle state for the given workspace + feature."""
    with _conn() as conn:
        row = conn.execute(
            """
            SELECT f.current_stage, f.feature_status, f.next_action
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

    return {
        "stage": row["current_stage"],
        "status": row["feature_status"],
        "next_action": row["next_action"],
    }
