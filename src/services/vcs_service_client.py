"""HTTP client for vcs-service (service-to-service).

hermes-agent calls vcs-service directly to create pull requests without
needing local git credentials or a GitHub PAT — vcs-service resolves a
scoped GitHub App installation token per-owner internally.

Configuration (env vars):
  VCS_SERVICE_URL    Base URL of vcs-service, e.g. http://localhost:8088.
                     If unset, create_pr raises VCSServiceError(reason_code="missing_config").
  VCS_SERVICE_TOKEN  Bearer token accepted by vcs-service's RequireServiceToken
                     middleware (must match vcs-service's INTERNAL_ACCESS_TOKEN).
                     If unset, same error.

Endpoint contracts (vcs-service), all under Authorization: Bearer <VCS_SERVICE_TOKEN>:

  POST /api/vcs/pr/create
    Body: {"owner", "repo", "title", "body"?, "head", "base", "draft"?}
    -> 201 {"number", "title", "body", "state", "html_url", "head_ref",
            "base_ref", "created_at", "updated_at"}

  POST /api/vcs/repo/ensure_branch
    Body: {"owner", "repo", "branch", "base_branch"}
    -> 200 {"status": "created"}
    Creates `branch` from `base_branch` if it doesn't already exist on the
    remote — the prerequisite for pr/create's `head`, which 422s if the
    branch isn't already pushed.

  POST /api/vcs/repo/commit_files
    Body: {"owner", "repo", "branch", "message", "files": {path: content},
           "base_branch"?}
    -> 200 {"status": "committed"}
    Commits directly to `branch` via GitHub's Contents API — no local git
    clone involved, unlike /repo/push (which requires a prior /repo/clone
    into a vcs-service-managed local workspace hermes-agent has no tool to
    write into, so it's intentionally not wired up here).

  POST /api/vcs/pr/diff              {"owner","repo","number"} -> {"diff": "<unified diff>"}
  POST /api/vcs/pr/files             {"owner","repo","number"} -> {"files": [...]}
  POST /api/vcs/repo/file_content    {"owner","repo","path","ref"?} -> {"path","content","sha","size"}
  POST /api/vcs/pr/metadata          {"owner","repo","number"} -> PR metadata
  POST /api/vcs/pr/comments          {"owner","repo","number"} -> issue + review comments
  POST /api/vcs/pr/review_history    {"owner","repo","number"} -> {"reviews": [...]}
  POST /api/vcs/pr/commits           {"owner","repo","number"} -> {"commits": [...]}
  POST /api/vcs/pr/checks            {"owner","repo","head_sha","poll_timeout_seconds"?}
                                      -> {"status", "check_runs": [...]}
  POST /api/vcs/pr/list              {"owner","repo","state"?,"head"?,"base"?,"per_page"?}
                                      -> {"prs": [...]}
  POST /api/vcs/pr/review_and_comment {"owner","repo","number","body","event","commit_id"?}
                                      -> {"review_url", "self_review_skipped"} — the
                                      two-call narrative pattern: an issue comment is
                                      always posted first, then the review event; a
                                      GitHub 422 (self-review restriction) on the review
                                      event is not an error here, it comes back as
                                      self_review_skipped=true.
  POST /api/vcs/repo/compare         {"owner","repo","base","head"} -> ahead/behind + commits + files

  All endpoints: 4xx/5xx -> {"error": "<message>"}  (vcs-service does not
  emit a machine reason_code today — the "error" string is the message itself)
"""

from __future__ import annotations

import logging
import os
from typing import Any

import aiohttp

logger = logging.getLogger(__name__)


class VCSServiceError(Exception):
    """Raised when vcs-service returns a non-2xx response or is misconfigured.

    Attributes:
        reason_code: Machine-readable code when vcs-service ever adds one,
            or a local sentinel (``missing_config``). Empty for ordinary
            HTTP errors today, since vcs-service's error body is a plain
            ``{"error": "<message>"}`` string, not a structured code.
        status: HTTP status code, 0 when the error is local (not from HTTP).
    """

    def __init__(self, message: str, *, reason_code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status


def check_vcs_service_available() -> bool:
    """Return True only when VCS_SERVICE_URL/TOKEN are configured."""
    return bool(os.environ.get("VCS_SERVICE_URL", "").strip()) and bool(
        os.environ.get("VCS_SERVICE_TOKEN", "").strip()
    )


def _resolve_config() -> tuple[str, str]:
    base_url = os.environ.get("VCS_SERVICE_URL", "").rstrip("/")
    if not base_url:
        raise VCSServiceError(
            "VCS_SERVICE_URL is not set — cannot call vcs-service.",
            reason_code="missing_config",
        )

    token = os.environ.get("VCS_SERVICE_TOKEN", "")
    if not token:
        raise VCSServiceError(
            "VCS_SERVICE_TOKEN is not set — cannot authenticate to vcs-service.",
            reason_code="missing_config",
        )
    return base_url, token


async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Shared HTTP plumbing for the vcs-service endpoints in this module.

    Returns the parsed JSON response body on success.

    Raises:
        VCSServiceError: On misconfiguration (``reason_code="missing_config"``)
            or any non-2xx HTTP response.
    """
    base_url, token = _resolve_config()
    url = f"{base_url}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session, session.post(
        url,
        headers=headers,
        json=payload,
        timeout=aiohttp.ClientTimeout(total=30),
    ) as resp:
        try:
            data = await resp.json(content_type=None)
        except Exception:
            data = {"raw": await resp.text()}

        if 200 <= resp.status < 300:
            return data

        error_message = data.get("error") if isinstance(data, dict) else None
        msg = (
            f"vcs-service returned HTTP {resp.status} for {url}: "
            f"{error_message or str(data)[:300]}"
        )
        logger.warning(msg)
        raise VCSServiceError(msg, status=resp.status)


async def create_pr(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    *,
    body: str = "",
    draft: bool = False,
) -> dict[str, Any]:
    """Create a pull request via POST {VCS_SERVICE_URL}/api/vcs/pr/create."""
    payload: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "title": title,
        "head": head,
        "base": base,
    }
    if body:
        payload["body"] = body
    if draft:
        payload["draft"] = True

    logger.info("vcs-service: creating PR %s/%s %s -> %s", owner, repo, head, base)
    return await _post("/api/vcs/pr/create", payload)


async def ensure_branch(
    owner: str,
    repo: str,
    branch: str,
    base_branch: str,
) -> dict[str, Any]:
    """Create `branch` from `base_branch` if it doesn't already exist, via
    POST {VCS_SERVICE_URL}/api/vcs/repo/ensure_branch.
    """
    logger.info(
        "vcs-service: ensuring branch %s/%s %s (base=%s)",
        owner,
        repo,
        branch,
        base_branch,
    )
    return await _post(
        "/api/vcs/repo/ensure_branch",
        {"owner": owner, "repo": repo, "branch": branch, "base_branch": base_branch},
    )


async def commit_files(
    owner: str,
    repo: str,
    branch: str,
    message: str,
    files: dict[str, str],
    *,
    base_branch: str = "",
) -> dict[str, Any]:
    """Commit `files` (path -> content) directly to `branch` via GitHub's
    Contents API, via POST {VCS_SERVICE_URL}/api/vcs/repo/commit_files.
    """
    payload: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "branch": branch,
        "message": message,
        "files": files,
    }
    if base_branch:
        payload["base_branch"] = base_branch

    logger.info(
        "vcs-service: committing %d file(s) to %s/%s@%s",
        len(files),
        owner,
        repo,
        branch,
    )
    return await _post("/api/vcs/repo/commit_files", payload)


async def get_pr_diff(owner: str, repo: str, number: int) -> str:
    """Return the unified diff for a PR via POST {VCS_SERVICE_URL}/api/vcs/pr/diff."""
    data = await _post("/api/vcs/pr/diff", {"owner": owner, "repo": repo, "number": number})
    return data.get("diff", "")


async def get_pr_files(owner: str, repo: str, number: int) -> dict[str, Any]:
    """Return the changed-file list via POST {VCS_SERVICE_URL}/api/vcs/pr/files."""
    return await _post("/api/vcs/pr/files", {"owner": owner, "repo": repo, "number": number})


async def get_file_at_ref(owner: str, repo: str, path: str, ref: str) -> dict[str, Any]:
    """Return a file's content at a ref via POST {VCS_SERVICE_URL}/api/vcs/repo/file_content.

    vcs-service decodes GitHub's base64 content server-side, so the returned
    "content" is already plain text.
    """
    return await _post(
        "/api/vcs/repo/file_content", {"owner": owner, "repo": repo, "path": path, "ref": ref}
    )


async def get_pr_metadata(owner: str, repo: str, number: int) -> dict[str, Any]:
    """Return PR metadata via POST {VCS_SERVICE_URL}/api/vcs/pr/metadata."""
    return await _post("/api/vcs/pr/metadata", {"owner": owner, "repo": repo, "number": number})


async def get_pr_comments(owner: str, repo: str, number: int) -> dict[str, Any]:
    """Return issue-level and review-level comments via POST /api/vcs/pr/comments."""
    return await _post("/api/vcs/pr/comments", {"owner": owner, "repo": repo, "number": number})


async def get_pr_reviews(owner: str, repo: str, number: int) -> dict[str, Any]:
    """Return PR review history via POST {VCS_SERVICE_URL}/api/vcs/pr/review_history."""
    return await _post(
        "/api/vcs/pr/review_history", {"owner": owner, "repo": repo, "number": number}
    )


async def get_pr_commits(owner: str, repo: str, number: int) -> dict[str, Any]:
    """Return PR commits via POST {VCS_SERVICE_URL}/api/vcs/pr/commits."""
    return await _post("/api/vcs/pr/commits", {"owner": owner, "repo": repo, "number": number})


async def get_check_runs(
    owner: str, repo: str, head_sha: str, *, poll_timeout_seconds: int = 60
) -> dict[str, Any]:
    """Return CI check-run results via POST {VCS_SERVICE_URL}/api/vcs/pr/checks.

    vcs-service performs the bounded poll server-side (blocking up to
    poll_timeout_seconds until all check-runs reach a terminal state).
    """
    return await _post(
        "/api/vcs/pr/checks",
        {
            "owner": owner,
            "repo": repo,
            "head_sha": head_sha,
            "poll_timeout_seconds": poll_timeout_seconds,
        },
    )


async def list_prs(
    owner: str,
    repo: str,
    *,
    state: str = "open",
    head: str = "",
    base: str = "",
    per_page: int = 30,
) -> dict[str, Any]:
    """List PRs for a repo via POST {VCS_SERVICE_URL}/api/vcs/pr/list."""
    payload: dict[str, Any] = {"owner": owner, "repo": repo, "state": state, "per_page": per_page}
    if head:
        payload["head"] = head
    if base:
        payload["base"] = base
    return await _post("/api/vcs/pr/list", payload)


async def compare_refs(owner: str, repo: str, base: str, head: str) -> dict[str, Any]:
    """Compare two refs via POST {VCS_SERVICE_URL}/api/vcs/repo/compare."""
    return await _post(
        "/api/vcs/repo/compare", {"owner": owner, "repo": repo, "base": base, "head": head}
    )


async def review_and_comment(
    owner: str,
    repo: str,
    number: int,
    body: str,
    event: str,
    *,
    commit_id: str = "",
    comments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Post the two-call PR review narrative pattern via POST
    {VCS_SERVICE_URL}/api/vcs/pr/review_and_comment.

    vcs-service posts the issue comment first (always attempted, fatal on
    failure), then the review event (with any inline `comments`, each
    {"path", "line", "body"}); a GitHub 422 on the review event (self-review
    restriction) comes back as a 200 with self_review_skipped=true rather
    than an error.
    """
    payload: dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "number": number,
        "body": body,
        "event": event,
    }
    if commit_id:
        payload["commit_id"] = commit_id
    if comments:
        payload["comments"] = comments
    return await _post("/api/vcs/pr/review_and_comment", payload)


def run_async(coro):
    """Bridge an async vcs_service_client coroutine into a sync call.

    Mirrors src.services.workflow_backend_client.run_async — uses the running
    agent event loop when available (production path, scheduled cross-thread
    via run_coroutine_threadsafe), else falls back to asyncio.run() for tests
    and non-agent callers.
    """
    import asyncio

    from plugins.context import get_agent_loop

    loop = get_agent_loop()
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=30)
    return asyncio.run(coro)
