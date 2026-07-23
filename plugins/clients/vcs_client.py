"""VCS service client for hermes-agent.

Provides a thin HTTP wrapper around vcs-service proxy endpoints so that
hermes-agent tools never call the GitHub API directly.  The vcs-service
manages authentication (GitHub App installation tokens) and provider-specific
routing internally — this client only needs the service URL.

Configuration
-------------
``VCS_SERVICE_URL``    — base URL of vcs-service (default: http://vcs-service:8080)
``VCS_SERVICE_TOKEN``  — Bearer token accepted by vcs-service's RequireServiceToken
                         middleware (must match vcs-service's INTERNAL_ACCESS_TOKEN).
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "http://vcs-service:8080"
_DEFAULT_TIMEOUT = 30

_PR_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")


def _base_url() -> str:
    return os.environ.get("VCS_SERVICE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _headers() -> Dict[str, str]:
    token = os.environ.get("VCS_SERVICE_TOKEN", "")
    if not token:
        raise RuntimeError(
            "VCS_SERVICE_TOKEN is not set — cannot authenticate to vcs-service."
        )
    return {"Authorization": f"Bearer {token}"}


def _post(path: str, payload: Dict[str, Any]) -> requests.Response:
    """POST *payload* as JSON to vcs-service; raises on HTTP errors."""
    url = f"{_base_url()}{path}"
    resp = requests.post(
        url,
        json=payload,
        headers=_headers(),
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def parse_pr_url(pr_url: str) -> Tuple[str, str, int]:
    """Extract (owner, repo, pull_number) from a GitHub PR URL.

    Raises ValueError when the URL does not match the expected pattern.
    """
    m = _PR_URL_RE.match(pr_url.strip())
    if not m:
        raise ValueError(
            f"Invalid GitHub PR URL {pr_url!r}. "
            "Expected https://github.com/{owner}/{repo}/pull/{number}."
        )
    return m.group(1), m.group(2), int(m.group(3))


# ---------------------------------------------------------------------------
# PR read operations
# ---------------------------------------------------------------------------


def get_pr_diff(owner: str, repo: str, pull_number: int) -> str:
    """Return the unified diff for the PR."""
    resp = _post(
        "/api/vcs/pr/diff",
        {"owner": owner, "repo": repo, "pull_number": pull_number},
    )
    data = resp.json()
    return data.get("diff", resp.text)


def get_pr_files(owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
    """Return the list of files changed in the PR."""
    resp = _post(
        "/api/vcs/pr/files",
        {"owner": owner, "repo": repo, "pull_number": pull_number},
    )
    return resp.json().get("files", resp.json())


def get_pr_metadata(owner: str, repo: str, pull_number: int) -> Dict[str, Any]:
    """Return PR metadata: title, body, author, branches, state, labels, etc.

    The vcs-service may return either a flat representation (already
    transformed) or the raw provider response (nested).  We normalise
    both shapes so callers always see the flat form.
    """
    resp = _post(
        "/api/vcs/pr/metadata",
        {"owner": owner, "repo": repo, "pull_number": pull_number},
    )
    data = resp.json()
    # If the response is already flat (has 'title' at top level), return as-is.
    if "title" in data and "author" in data and "base_branch" in data:
        return data
    # Otherwise treat it as a raw provider response and flatten it.
    return {
        "number": data.get("number"),
        "title": data.get("title"),
        "body": data.get("body"),
        "state": data.get("state"),
        "draft": data.get("draft"),
        "author": data.get("user", {}).get("login"),
        "base_branch": data.get("base", {}).get("ref"),
        "base_sha": data.get("base", {}).get("sha"),
        "head_branch": data.get("head", {}).get("ref"),
        "head_sha": data.get("head", {}).get("sha"),
        "labels": [lb.get("name") for lb in data.get("labels", [])],
        "requested_reviewers": [
            r.get("login") for r in data.get("requested_reviewers", [])
        ],
        "merged": data.get("merged"),
        "merged_at": data.get("merged_at"),
        "html_url": data.get("html_url"),
        "created_at": data.get("created_at"),
        "updated_at": data.get("updated_at"),
    }


def get_pr_comments(owner: str, repo: str, pull_number: int) -> Dict[str, Any]:
    """Return issue-level and review-level comments on the PR."""
    resp = _post(
        "/api/vcs/pr/comments",
        {"owner": owner, "repo": repo, "pull_number": pull_number},
    )
    return resp.json()


def get_pr_reviews(owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
    """Return review history for the PR."""
    resp = _post(
        "/api/vcs/pr/reviews/list",
        {"owner": owner, "repo": repo, "pull_number": pull_number},
    )
    return resp.json().get("reviews", resp.json())


def get_pr_commits(owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
    """Return the commits in the PR."""
    resp = _post(
        "/api/vcs/pr/commits",
        {"owner": owner, "repo": repo, "pull_number": pull_number},
    )
    return resp.json().get("commits", resp.json())


def get_check_runs(
    owner: str,
    repo: str,
    head_sha: str,
    poll_timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Return CI check-run results for *head_sha*, with optional bounded poll.

    When *poll_timeout_seconds* is set (> 0), polls every 15 seconds until all
    check-runs reach a terminal state or the timeout expires.  On timeout
    Returns ``status: "pending"`` rather than blocking forever.
    """

    if poll_timeout_seconds is None:
        poll_timeout_seconds = int(
            os.environ.get("CHAT_REVIEW_CI_POLL_TIMEOUT_SECONDS", "60")
        )

    _TERMINAL = frozenset(
        {"success", "failure", "cancelled", "skipped", "neutral", "action_required"}
    )
    _POLL_INTERVAL = 15

    deadline = time.monotonic() + poll_timeout_seconds

    while True:
        resp = _post(
            "/api/vcs/pr/checks",
            {"owner": owner, "repo": repo, "head_sha": head_sha},
        )
        data = resp.json()
        runs = data.get("check_runs", [])

        formatted = [
            {
                "name": r.get("name"),
                "status": r.get("status"),
                "conclusion": r.get("conclusion"),
                "html_url": r.get("html_url"),
                "started_at": r.get("started_at"),
                "completed_at": r.get("completed_at"),
            }
            for r in runs
        ]

        # No check-runs means CI isn't configured — treat as passed.
        if not runs:
            return {"status": "no_checks", "check_runs": formatted}

        all_terminal = all(r.get("conclusion") in _TERMINAL for r in runs)
        if all_terminal:
            any_failed = any(
                r.get("conclusion") in ("failure", "cancelled") for r in runs
            )
            return {
                "status": "failed" if any_failed else "passed",
                "check_runs": formatted,
            }

        # Check timeout before sleeping.
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {"status": "pending", "check_runs": formatted}

        time.sleep(min(_POLL_INTERVAL, remaining))


def compare_refs(owner: str, repo: str, base: str, head: str) -> Dict[str, Any]:
    """Compare two refs/branches/commits."""
    resp = _post(
        "/api/vcs/repo/compare",
        {"owner": owner, "repo": repo, "base": base, "head": head},
    )
    return resp.json()


def get_file_at_ref(owner: str, repo: str, path: str, ref: str) -> Dict[str, Any]:
    """Return the content of a file at a given ref/commit/branch.

    If the response contains base64-encoded content we decode it before
    returning, matching the historical behaviour of github_pr_client.
    """
    import base64

    resp = _post(
        "/api/vcs/repo/file_content",
        {"owner": owner, "repo": repo, "path": path, "ref": ref},
    )
    data = resp.json()

    if isinstance(data, list):
        # Path is a directory, not a file.
        return {
            "ok": False,
            "error": f"{path!r} is a directory, not a file.",
        }

    raw = data.get("content", "")
    if data.get("encoding") == "base64" and raw:
        try:
            content = base64.b64decode(raw).decode("utf-8", errors="replace")
        except Exception:
            content = raw
    else:
        content = raw

    return {
        "path": data.get("path"),
        "sha": data.get("sha"),
        "size": data.get("size"),
        "encoding": data.get("encoding"),
        "content": content,
        "html_url": data.get("html_url"),
        "ok": data.get("ok", True),
    }


def list_open_prs(
    owner: str,
    repo: str,
    state: str = "open",
    head: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """List PRs for a repo."""
    payload: Dict[str, Any] = {"owner": owner, "repo": repo, "state": state}
    if head:
        payload["head"] = head
    resp = _post("/api/vcs/pr/list", payload)
    return resp.json().get("pull_requests", resp.json())


# ---------------------------------------------------------------------------
# PR write operations
# ---------------------------------------------------------------------------


def post_issue_comment(
    owner: str, repo: str, issue_number: int, body: str
) -> Dict[str, Any]:
    """Post *body* as an issue comment on PR *issue_number*."""
    resp = _post(
        "/api/vcs/pr/issues/comments",
        {
            "owner": owner,
            "repo": repo,
            "issue_number": issue_number,
            "body": body,
        },
    )
    resp.raise_for_status()
    return resp.json()


def post_pr_review(
    owner: str,
    repo: str,
    pull_number: int,
    event: str,
    body: str,
    comments: Optional[List[Dict[str, Any]]] = None,
) -> requests.Response:
    """Post a formal review event on PR *pull_number*.

    Returns the raw ``requests.Response`` so callers can check the status code
    (201 success vs 422 self-review restriction) without raising.
    """
    payload: Dict[str, Any] = {
        "owner": owner,
        "repo": repo,
        "pull_number": pull_number,
        "event": event,
        "body": body,
    }
    if comments:
        payload["comments"] = comments
    url = f"{_base_url()}/api/vcs/pr/reviews"
    return requests.post(
        url, json=payload, headers=_headers(), timeout=_DEFAULT_TIMEOUT
    )
