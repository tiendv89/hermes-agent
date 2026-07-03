"""HTTP client for workflow-backend task creation (service-to-service).

hermes-agent calls workflow-backend directly (no BFF) to create tasks for
go-owned features at tasks-stage approval.

Configuration (env vars):
  WORKFLOW_BACKEND_URL            Base URL of workflow-backend,
                                  e.g. http://workflow-backend:8080.
                                  If unset, create_feature_tasks raises
                                  WorkflowBackendError(reason_code="missing_config").
  WORKFLOW_BACKEND_SERVICE_TOKEN  Bearer token accepted by RequireBFFIdentity.
                                  If unset, same error.

Endpoint contract (workflow-backend, T3 guard):
  POST {WORKFLOW_BACKEND_URL}/api/workspaces/{workspace_id}/features/{feature_id}/tasks
  Headers:
    Authorization: Bearer <WORKFLOW_BACKEND_SERVICE_TOKEN>
    X-User-Id: <caller user_id from T1-threaded context>
    X-Org-Id: <caller org_id from T1-threaded context>
    X-Accessible-Org-Ids: <org_id> (single-org action)
  Body: {"tasks": [{"id": "T1", "title": "...", "depends_on": [], ...}, ...]}
  → 200/201  {"tasks": [...]}
  → 4xx      {"error": "<reason_code>", "message": "..."}
              reason codes: feature_not_tasks_approved, tasks_already_exist
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List

import aiohttp

logger = logging.getLogger(__name__)

# Matches a data row in the tasks.md Index table:
#   | T1 | 1 | Title text | hermes-agent | — |
_INDEX_ROW_RE = re.compile(
    r"^\|\s*(T\d+)\s*\|\s*\d+\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$"
)


class WorkflowBackendError(Exception):
    """Raised when workflow-backend returns a non-2xx response or is misconfigured.

    Attributes:
        reason_code: Machine-readable code from the backend (e.g.
            ``feature_not_tasks_approved``, ``tasks_already_exist``) or a
            local sentinel (``missing_config``, ``empty_tasks``).
        status: HTTP status code, 0 when the error is local (not from HTTP).
    """

    def __init__(self, message: str, *, reason_code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status


def parse_tasks_md_index(tasks_md: str) -> List[Dict[str, Any]]:
    """Parse the ``## Index`` table from a tasks.md string.

    Returns a list of task dicts with keys:
      - ``id``: task ID string, e.g. ``"T1"``
      - ``title``: task title (backtick code spans stripped)
      - ``repo``: repo slug, e.g. ``"hermes-agent"``
      - ``depends_on``: list of task ID strings (empty list when the cell is ``—``)
      - ``actor_type``: always ``"agent"`` (default; callers may override)
    """
    tasks: List[Dict[str, Any]] = []
    in_table = False
    separator_seen = False

    for line in tasks_md.splitlines():
        stripped = line.strip()

        if not stripped.startswith("|"):
            if in_table:
                break  # first non-pipe line after the table ends it
            continue

        # Detect the index table header row (contains "| ID |")
        if not in_table:
            if "| ID |" in stripped or "| ID|" in stripped or "|ID |" in stripped:
                in_table = True
            continue

        # Skip the separator row (|----|---...)
        if not separator_seen:
            separator_seen = True
            continue

        m = _INDEX_ROW_RE.match(stripped)
        if not m:
            continue

        task_id = m.group(1)
        title_raw = m.group(2).strip()
        repo = m.group(3).strip()
        depends_raw = m.group(4).strip()

        # Strip inline code backticks from the title
        title = re.sub(r"`([^`]*)`", r"\1", title_raw).strip()

        # Parse depends_on: "—" or "-" or empty → []; else comma-separated IDs
        if depends_raw in ("—", "-", ""):
            depends_on: List[str] = []
        else:
            depends_on = [
                d.strip()
                for d in re.split(r"[,\s]+", depends_raw)
                if re.match(r"^T\d+$", d.strip())
            ]

        tasks.append(
            {
                "id": task_id,
                "title": title,
                "repo": repo,
                "depends_on": depends_on,
                "actor_type": "agent",
            }
        )

    return tasks


def _build_headers(user_id: str, org_id: str, token: str) -> Dict[str, str]:
    """Build HTTP headers for a workflow-backend service-to-service call."""
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-User-Id": user_id,
        "X-Org-Id": org_id,
        "X-Accessible-Org-Ids": org_id,
    }


def _extract_reason_code(body: Any) -> str:
    """Pull the machine-readable reason code out of a backend error response body."""
    if not isinstance(body, dict):
        return ""
    # Try common field names in order of preference
    for key in ("reason_code", "reason", "error_code", "code", "error"):
        val = body.get(key)
        if isinstance(val, str) and val:
            return val
        if isinstance(val, dict):
            # Nested: {"error": {"code": "...", "message": "..."}}
            inner = val.get("code") or val.get("reason_code") or val.get("reason") or ""
            if inner:
                return inner
    return ""


async def create_feature_tasks(
    workspace_id: str,
    feature_id: str,
    tasks_md: str,
) -> Dict[str, Any]:
    """POST bulk tasks to workflow-backend for a go-owned feature.

    Parses the tasks.md Index table, builds the request payload, and calls:
      POST {WORKFLOW_BACKEND_URL}/api/workspaces/{workspace_id}/features/{feature_id}/tasks

    Identity headers (X-User-Id / X-Org-Id) come from the T1-threaded context
    (``plugins.context.get_user_id`` / ``get_org_id``).

    Args:
        workspace_id: Workspace slug or UUID.
        feature_id: Feature slug or UUID.
        tasks_md: Content of the approved tasks.md (must contain the Index table).

    Returns:
        The parsed JSON response body from workflow-backend on success.

    Raises:
        WorkflowBackendError: On misconfiguration (``reason_code="missing_config"``),
            no tasks parsed (``reason_code="empty_tasks"``), or any non-2xx HTTP
            response. The backend's reason code is surfaced verbatim.
    """
    from plugins.context import get_org_id, get_user_id

    base_url = os.environ.get("WORKFLOW_BACKEND_URL", "").rstrip("/")
    if not base_url:
        raise WorkflowBackendError(
            "WORKFLOW_BACKEND_URL is not set — cannot create tasks.",
            reason_code="missing_config",
        )

    token = os.environ.get("WORKFLOW_BACKEND_SERVICE_TOKEN", "")
    if not token:
        raise WorkflowBackendError(
            "WORKFLOW_BACKEND_SERVICE_TOKEN is not set — cannot authenticate to workflow-backend.",
            reason_code="missing_config",
        )

    tasks = parse_tasks_md_index(tasks_md)
    if not tasks:
        raise WorkflowBackendError(
            "No tasks found in the tasks.md Index table — cannot create tasks.",
            reason_code="empty_tasks",
        )

    user_id = get_user_id()
    org_id = get_org_id()
    headers = _build_headers(user_id, org_id, token)
    url = f"{base_url}/api/workspaces/{workspace_id}/features/{feature_id}/tasks"
    payload: Dict[str, Any] = {"tasks": tasks}

    logger.info(
        "workflow-backend: creating %d task(s) for feature %s/%s",
        len(tasks),
        workspace_id,
        feature_id,
    )

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            headers=headers,
            json=payload,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {"raw": await resp.text()}

            if 200 <= resp.status < 300:
                logger.info(
                    "workflow-backend: tasks created for %s/%s (status=%d)",
                    workspace_id,
                    feature_id,
                    resp.status,
                )
                return body

            reason_code = _extract_reason_code(body)
            msg = (
                f"workflow-backend returned HTTP {resp.status} for {url}"
                + (f" [reason={reason_code}]" if reason_code else "")
                + f": {str(body)[:300]}"
            )
            logger.warning(msg)
            raise WorkflowBackendError(msg, reason_code=reason_code, status=resp.status)
