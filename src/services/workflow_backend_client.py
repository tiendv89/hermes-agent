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
from typing import Any, Dict, List

import aiohttp

logger = logging.getLogger(__name__)


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
    tasks: List[Dict[str, Any]],
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> Dict[str, Any]:
    """POST an already-parsed bulk task list to workflow-backend for a go feature.

    Parsing tasks.md is the caller's responsibility (see the ``parse_tasks``
    tool / ``parse_tasks_index``); this function only builds the request payload
    and calls:
      POST {WORKFLOW_BACKEND_URL}/api/workspaces/{workspace_id}/features/{feature_id}/tasks

    Identity headers (X-User-Id / X-Org-Id): callers should pass ``user_id`` /
    ``org_id`` explicitly, captured on the thread where the request context is
    set. This coroutine may run on a different thread than the tool handler
    (it is scheduled on the agent event loop via ``run_coroutine_threadsafe``),
    and the caller identity is stored in ``threading.local`` — so reading it
    here would yield empty values. When not passed, we fall back to
    ``plugins.context`` getters for same-thread callers and tests.

    Args:
        workspace_id: Workspace slug or UUID.
        feature_id: Feature slug or UUID.
        tasks: Parsed task rows (each: ``name``, ``title``, ``repo``,
            ``depends_on``, ``actor_type``) as produced by ``parse_tasks_index``.
        user_id: Caller user id for the X-User-Id header. Falls back to
            ``plugins.context.get_user_id()`` when ``None``.
        org_id: Caller org id for the X-Org-Id / X-Accessible-Org-Ids headers.
            Falls back to ``plugins.context.get_org_id()`` when ``None``.

    Returns:
        The parsed JSON response body from workflow-backend on success.

    Raises:
        WorkflowBackendError: On misconfiguration (``reason_code="missing_config"``),
            an empty task list (``reason_code="empty_tasks"``), or any non-2xx HTTP
            response. The backend's reason code is surfaced verbatim.
    """
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

    if not tasks:
        raise WorkflowBackendError(
            "No tasks to create — the parsed task list is empty.",
            reason_code="empty_tasks",
        )

    if user_id is None or org_id is None:
        from plugins.context import get_org_id, get_user_id

        if user_id is None:
            user_id = get_user_id()
        if org_id is None:
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
