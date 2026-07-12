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


def run_async(coro):
    """Bridge an async workflow-backend-client coroutine into a sync call.

    Uses the running agent event loop when available (production path via
    ``get_agent_loop`` — the coroutine is scheduled cross-thread via
    ``run_coroutine_threadsafe``), else falls back to ``asyncio.run()`` for
    tests and non-agent callers. This is the same bridge
    ``_run_async_create_tasks`` (plugins/tools/approve.py) used privately
    before every ``plugins.db`` caller needed the same plumbing — callers in
    a synchronous context (e.g. plugins/tools/*.py tool handlers) should wrap
    their coroutine in this; callers already running inside an event loop
    (FastAPI routers) should ``await`` the client function directly instead.
    """
    import asyncio

    from plugins.context import get_agent_loop

    loop = get_agent_loop()
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=30)
    return asyncio.run(coro)


async def _call(
    method: str,
    path: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
    json_body: Dict[str, Any] | None = None,
    not_found_message: str | None = None,
) -> Any:
    """Shared HTTP plumbing for the workflow-backend endpoints that replace
    ``plugins/db.py``'s direct Postgres access (workspace/feature/task reads,
    the feature-by-id lookup, and the stage-update write).

    Reuses the same env vars, header construction, and timeout/error
    conventions as ``create_feature_tasks``. Unwraps the
    ``{"success": true, "data": ...}`` envelope every workflow-backend
    endpoint returns, so callers get the same plain dict/list shapes the
    plugins.db functions they replace used to return.

    When ``not_found_message`` is given, a 404 response raises
    ``ValueError(not_found_message)`` instead of ``WorkflowBackendError`` —
    matching plugins.db's contract, since several existing callers
    specifically ``except ValueError`` around these lookups.
    """
    base_url = os.environ.get("WORKFLOW_BACKEND_URL", "").rstrip("/")
    if not base_url:
        raise WorkflowBackendError(
            "WORKFLOW_BACKEND_URL is not set — cannot call workflow-backend.",
            reason_code="missing_config",
        )

    token = os.environ.get("WORKFLOW_BACKEND_SERVICE_TOKEN", "")
    if not token:
        raise WorkflowBackendError(
            "WORKFLOW_BACKEND_SERVICE_TOKEN is not set — cannot authenticate to workflow-backend.",
            reason_code="missing_config",
        )

    if user_id is None or org_id is None:
        from plugins.context import get_org_id, get_user_id

        if user_id is None:
            user_id = get_user_id()
        if org_id is None:
            org_id = get_org_id()

    headers = _build_headers(user_id, org_id, token)
    url = f"{base_url}{path}"

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method,
            url,
            headers=headers,
            json=json_body,
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except Exception:
                body = {"raw": await resp.text()}

            if 200 <= resp.status < 300:
                if isinstance(body, dict) and "data" in body:
                    return body["data"]
                return body

            if resp.status == 404 and not_found_message is not None:
                raise ValueError(not_found_message)

            reason_code = _extract_reason_code(body)
            msg = (
                f"workflow-backend returned HTTP {resp.status} for {url}"
                + (f" [reason={reason_code}]" if reason_code else "")
                + f": {str(body)[:300]}"
            )
            logger.warning(msg)
            raise WorkflowBackendError(msg, reason_code=reason_code, status=resp.status)


def check_workflow_available() -> bool:
    """Return True only when WORKFLOW_BACKEND_URL/SERVICE_TOKEN are configured."""
    return bool(os.environ.get("WORKFLOW_BACKEND_URL", "").strip()) and bool(
        os.environ.get("WORKFLOW_BACKEND_SERVICE_TOKEN", "").strip()
    )


async def get_workspace_context(
    workspace_id: str, *, user_id: str | None = None, org_id: str | None = None
) -> Dict[str, Any]:
    """Return workspace metadata shaped for plugins tool consumers.

    Replaces plugins.db.get_workspace_context. workspace_id must be the
    workspace UUID (GET /api/workspaces/:id does not accept a slug).
    """
    data = await _call(
        "GET",
        f"/api/workspaces/{workspace_id}",
        user_id=user_id,
        org_id=org_id,
        not_found_message=f"Workspace not found: {workspace_id!r}",
    )
    repo_url = data.get("repo_url") or ""
    management_repo_id = data.get("management_repo_id") or ""
    repos = [{"id": management_repo_id, "github": repo_url}] if repo_url else []
    return {"management_repo": management_repo_id, "repos": repos}


async def get_workspace_organization_id(
    workspace_id: str, *, user_id: str | None = None, org_id: str | None = None
) -> str | None:
    """Return the organization_id owning workspace_id, or None if not found.

    Replaces plugins.db.get_workspace_organization_id.
    """
    try:
        data = await _call(
            "GET", f"/api/workspaces/{workspace_id}", user_id=user_id, org_id=org_id
        )
    except WorkflowBackendError as exc:
        if exc.status == 404:
            return None
        raise
    return data.get("organization_id") or None


async def get_workspace_slug(
    workspace_id: str, *, user_id: str | None = None, org_id: str | None = None
) -> str:
    """Resolve a workspace UUID to its canonical slug, or "" if not found.

    Replaces plugins.db.get_workspace_slug.
    """
    try:
        data = await _call(
            "GET", f"/api/workspaces/{workspace_id}", user_id=user_id, org_id=org_id
        )
    except WorkflowBackendError as exc:
        if exc.status == 404:
            return ""
        raise
    return data.get("slug") or ""


async def resolve_workspace_slug(
    workspace_id: str, *, user_id: str | None = None, org_id: str | None = None
) -> str:
    """Best-effort normalize workspace_id to its canonical slug.

    Replaces plugins.db.resolve_workspace_slug. Falls back to the raw value
    when workflow-backend is unavailable or the lookup misses/errors, so
    callers degrade to passthrough instead of failing outright.
    """
    if not workspace_id or not check_workflow_available():
        return workspace_id
    try:
        slug = await get_workspace_slug(workspace_id, user_id=user_id, org_id=org_id)
        return slug or workspace_id
    except Exception:
        logger.debug(
            "resolve_workspace_slug: lookup failed for %r", workspace_id, exc_info=True
        )
        return workspace_id


async def get_workspace_id_for_feature(
    feature_id: str, *, user_id: str | None = None, org_id: str | None = None
) -> str:
    """Resolve the workspace_id (UUID) owning feature_id, with no workspace
    known in advance.

    Replaces plugins.db.get_workspace_id_for_feature. feature_id must be the
    feature's business-key UUID (GET /api/features/:id does not accept the
    feature_name slug — matching workflow-backend's existing UUID-only
    convention for feature lookups).
    """
    data = await _call(
        "GET",
        f"/api/features/{feature_id}",
        user_id=user_id,
        org_id=org_id,
        not_found_message=f"Feature not found: {feature_id!r}",
    )
    return data["workspace_id"]


async def _resolve_feature_id_by_name(
    workspace_id: str, name: str, *, user_id: str | None, org_id: str | None
) -> str | None:
    """Resolve a feature_name slug to its business-key UUID via the feature
    search endpoint (exact name match), or None if no such feature exists.

    workflow-backend's feature-detail/tasks/stage endpoints only accept the
    UUID (feature_name lookups are intentionally unsupported at that layer),
    unlike plugins.db's functions, which historically accepted either.
    """
    from urllib.parse import quote

    data = await _call(
        "GET",
        f"/api/workspaces/{workspace_id}/features?name={quote(name)}",
        user_id=user_id,
        org_id=org_id,
    )
    items = data.get("items") or []
    return items[0]["id"] if items else None


async def get_feature_detail(
    workspace_id: str,
    feature_id: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> Dict[str, Any]:
    """Return feature metadata and lifecycle state for the given workspace + feature.

    Replaces plugins.db.get_feature_detail. feature_id may be the business-key
    UUID (the common case — resolved in a single round trip) or a feature_name
    slug (resolved via a name-search fallback on a 404, matching plugins.db's
    original "slug or UUID" acceptance).
    """
    try:
        data = await _call(
            "GET",
            f"/api/workspaces/{workspace_id}/features/{feature_id}",
            user_id=user_id,
            org_id=org_id,
            not_found_message=f"Feature {feature_id!r} not found in workspace {workspace_id!r}",
        )
    except ValueError:
        resolved_id = await _resolve_feature_id_by_name(workspace_id, feature_id, user_id=user_id, org_id=org_id)
        if resolved_id is None:
            raise
        data = await _call(
            "GET",
            f"/api/workspaces/{workspace_id}/features/{resolved_id}",
            user_id=user_id,
            org_id=org_id,
            not_found_message=f"Feature {feature_id!r} not found in workspace {workspace_id!r}",
        )
    return {
        "feature_name": data.get("feature_name"),
        "title": data.get("title"),
        "stage": data.get("current_stage"),
        "status": data.get("status"),
        "next_action": data.get("next_action"),
        "owner": data.get("owner"),
        "init_pr_url": data.get("init_pr_url"),
        "stages": data.get("stages") or {},
    }


async def get_feature_tasks(
    workspace_id: str,
    feature_id: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> List[Dict[str, Any]]:
    """Return all tasks for the given workspace + feature.

    Replaces plugins.db.get_feature_tasks. Reuses the feature-detail endpoint
    (it already embeds the full unpaginated task list) rather than the
    separate paginated search endpoint.
    """
    data = await _call(
        "GET",
        f"/api/workspaces/{workspace_id}/features/{feature_id}",
        user_id=user_id,
        org_id=org_id,
        not_found_message=f"Feature {feature_id!r} not found in workspace {workspace_id!r}",
    )
    tasks = data.get("tasks") or []
    return [
        {
            "task_name": t.get("task_name"),
            "title": t.get("title"),
            "status": t.get("status"),
            "blocked_reason": t.get("blocked_reason"),
            "depends_on": t.get("depends_on"),
            "pr": t.get("pr"),
            "execution": t.get("execution"),
        }
        for t in tasks
    ]


async def update_feature_stage(
    workspace_id: str,
    feature_id: str,
    stage: str,
    review_status: str,
    feature_status: str,
    current_stage: str,
    next_action: str,
    actor: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> None:
    """Persist stage-review state (approve/reject/reopen) for one stage of a
    feature's lifecycle.

    Replaces plugins.db.update_feature_stage. The JSONB stages merge (touch
    only this stage's key, append to review_history) now happens server-side
    in workflow-backend, inside a single locked transaction.
    """
    await _call(
        "PATCH",
        f"/api/workspaces/{workspace_id}/features/{feature_id}/stage",
        user_id=user_id,
        org_id=org_id,
        json_body={
            "stage": stage,
            "review_status": review_status,
            "feature_status": feature_status,
            "current_stage": current_stage,
            "next_action": next_action,
            "actor": actor,
        },
        not_found_message=f"Feature {feature_id!r} not found in workspace {workspace_id!r}",
    )


async def create_feature(
    workspace_id: str,
    name: str,
    *,
    description: str = "",
    start_stage: str | None = None,
    user_id: str | None = None,
    org_id: str | None = None,
) -> Dict[str, Any]:
    """Create a new go-owned feature via POST /api/workspaces/:workspaceId/features.

    Always sends owner="go" — the agent entry point is go-only by design.
    Returns the parsed response body on success.
    Raises WorkflowBackendError on any non-2xx response.
    """
    body: Dict[str, Any] = {"name": name, "description": description, "owner": "go"}
    if start_stage:
        body["start_stage"] = start_stage
    return await _call(
        "POST",
        f"/api/workspaces/{workspace_id}/features",
        user_id=user_id,
        org_id=org_id,
        json_body=body,
    )


async def activate_ready_tasks(
    workspace_id: str,
    feature_id: str,
    *,
    user_id: str | None = None,
    org_id: str | None = None,
) -> List[str]:
    """Transition every "todo" task whose dependencies are all "done" to
    "ready". Returns the task_names it activated (may be empty).

    Replaces plugins.tools.approve._activate_tasks_db's direct SQL. Called at
    tasks-stage approval so dependency-free (or now-unblocked) tasks become
    claimable immediately.
    """
    data = await _call(
        "POST",
        f"/api/workspaces/{workspace_id}/features/{feature_id}/tasks/activate-ready",
        user_id=user_id,
        org_id=org_id,
        not_found_message=f"Feature {feature_id!r} not found in workspace {workspace_id!r}",
    )
    return data.get("activated") or []
