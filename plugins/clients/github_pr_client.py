"""Shared GitHub PR client for hermes-agent.

Implements GitHub REST API auth/header handling for implementation-repo PR
endpoints (PR review: diff, comments, checks, posting reviews). All functions
are synchronous and use the ``requests`` library already present as a
hermes-agent dependency.

Exported functions are consumed by ``plugins/tools/github_pr_context.py`` and
(in T2) ``plugins/tools/github_pr_review.py``.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_GITHUB_API_URL = "https://api.github.com"
_DEFAULT_TIMEOUT = 30

_PR_URL_RE = re.compile(
    r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)"
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _token() -> str:
    return os.environ.get("GITHUB_TOKEN", "").strip()


def _headers(accept: str = "application/vnd.github.v3+json") -> Dict[str, str]:
    tok = _token()
    h = {"Accept": accept}
    if tok:
        h["Authorization"] = f"token {tok}"
    return h


def parse_pr_url(pr_url: str) -> Tuple[str, str, int]:
    """Extract (owner, repo, pull_number) from a GitHub PR URL.

    Raises ValueError when the URL does not match the expected pattern.
    """
    m = _PR_URL_RE.match(pr_url.strip())
    if not m:
        raise ValueError(
            f"Invalid GitHub PR URL {pr_url!r}. "
            "Expected https://github.com/{{owner}}/{{repo}}/pull/{{number}}."
        )
    return m.group(1), m.group(2), int(m.group(3))


def _get(url: str, params: Optional[Dict[str, Any]] = None, accept: str = "application/vnd.github.v3+json") -> requests.Response:
    resp = requests.get(
        url,
        headers=_headers(accept),
        params=params,
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    return resp


def _post(url: str, payload: Dict[str, Any]) -> requests.Response:
    """POST *payload* as JSON; returns the raw response (caller checks status)."""
    return requests.post(
        url,
        headers={**_headers(), "Content-Type": "application/json"},
        json=payload,
        timeout=_DEFAULT_TIMEOUT,
    )


# ---------------------------------------------------------------------------
# PR-level read functions
# ---------------------------------------------------------------------------


def get_pr_metadata(owner: str, repo: str, pull_number: int) -> Dict[str, Any]:
    """Return PR metadata: title, body, author, branches, state, labels, etc."""
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}"
    data = _get(url).json()
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


def get_pr_diff(owner: str, repo: str, pull_number: int) -> str:
    """Return the unified diff for the PR as a string."""
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}"
    resp = _get(url, accept="application/vnd.github.v3.diff")
    return resp.text


def get_pr_files(owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
    """Return the list of files changed in the PR."""
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}/files"
    data = _get(url).json()
    return [
        {
            "filename": f.get("filename"),
            "status": f.get("status"),
            "additions": f.get("additions"),
            "deletions": f.get("deletions"),
            "changes": f.get("changes"),
        }
        for f in data
    ]


def get_pr_comments(owner: str, repo: str, pull_number: int) -> Dict[str, Any]:
    """Return both issue-level and review-level (inline) comments on the PR."""
    issue_url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/issues/{pull_number}/comments"
    review_url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}/comments"

    issue_comments = [
        {
            "id": c.get("id"),
            "user": c.get("user", {}).get("login"),
            "body": c.get("body"),
            "created_at": c.get("created_at"),
            "html_url": c.get("html_url"),
        }
        for c in _get(issue_url).json()
    ]
    review_comments = [
        {
            "id": c.get("id"),
            "user": c.get("user", {}).get("login"),
            "body": c.get("body"),
            "path": c.get("path"),
            "line": c.get("line"),
            "created_at": c.get("created_at"),
            "html_url": c.get("html_url"),
        }
        for c in _get(review_url).json()
    ]
    return {
        "issue_comments": issue_comments,
        "review_comments": review_comments,
    }


def get_pr_reviews(owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
    """Return review history for the PR (who reviewed, verdict, when)."""
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
    data = _get(url).json()
    return [
        {
            "id": r.get("id"),
            "user": r.get("user", {}).get("login"),
            "state": r.get("state"),
            "body": r.get("body"),
            "submitted_at": r.get("submitted_at"),
            "html_url": r.get("html_url"),
        }
        for r in data
    ]


def get_pr_commits(owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
    """Return the commits in the PR."""
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}/commits"
    data = _get(url).json()
    return [
        {
            "sha": c.get("sha"),
            "message": c.get("commit", {}).get("message"),
            "author": c.get("commit", {}).get("author", {}).get("name"),
            "author_email": c.get("commit", {}).get("author", {}).get("email"),
            "date": c.get("commit", {}).get("author", {}).get("date"),
            "html_url": c.get("html_url"),
        }
        for c in data
    ]


def get_check_runs(
    owner: str,
    repo: str,
    head_sha: str,
    poll_timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Return CI check-run results for *head_sha*, with an optional bounded poll.

    When *poll_timeout_seconds* is set (> 0), the function polls every 15 seconds
    until all check-runs reach a terminal state or the timeout expires.  On
    timeout it returns ``status: "pending"`` rather than blocking forever.

    Terminal conclusions: success, failure, cancelled, skipped, neutral, action_required.
    In-progress states: None (queued/in_progress).
    """
    if poll_timeout_seconds is None:
        poll_timeout_seconds = int(
            os.environ.get("CHAT_REVIEW_CI_POLL_TIMEOUT_SECONDS", "60")
        )

    _TERMINAL = frozenset(
        {"success", "failure", "cancelled", "skipped", "neutral", "action_required"}
    )
    _POLL_INTERVAL = 15

    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/commits/{head_sha}/check-runs"
    deadline = time.monotonic() + poll_timeout_seconds

    while True:
        data = _get(url).json()
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


# ---------------------------------------------------------------------------
# Commit-level read functions
# ---------------------------------------------------------------------------


def compare_refs(
    owner: str,
    repo: str,
    base: str,
    head: str,
) -> Dict[str, Any]:
    """Compare two refs/branches/commits and return ahead/behind counts + diff."""
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/compare/{base}...{head}"
    data = _get(url).json()
    return {
        "status": data.get("status"),
        "ahead_by": data.get("ahead_by"),
        "behind_by": data.get("behind_by"),
        "total_commits": data.get("total_commits"),
        "commits": [
            {
                "sha": c.get("sha"),
                "message": c.get("commit", {}).get("message"),
                "author": c.get("commit", {}).get("author", {}).get("name"),
                "date": c.get("commit", {}).get("author", {}).get("date"),
            }
            for c in data.get("commits", [])
        ],
        "files": [
            {
                "filename": f.get("filename"),
                "status": f.get("status"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
            }
            for f in data.get("files", [])
        ],
    }


# ---------------------------------------------------------------------------
# Repo/file-level read functions
# ---------------------------------------------------------------------------


def get_file_at_ref(
    owner: str,
    repo: str,
    path: str,
    ref: str,
) -> Dict[str, Any]:
    """Return the content of a file at a given ref/commit/branch.

    GitHub returns the content base64-encoded; this function decodes it and
    returns the plain text content alongside the SHA and size.
    """
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
    data = _get(url, params={"ref": ref}).json()

    if isinstance(data, list):
        # Path is a directory, not a file.
        return {
            "ok": False,
            "error": f"{path!r} is a directory, not a file.",
        }

    raw = data.get("content", "")
    try:
        content = base64.b64decode(raw).decode("utf-8", errors="replace")
    except Exception:
        content = raw

    return {
        "path": data.get("path"),
        "sha": data.get("sha"),
        "size": data.get("size"),
        "encoding": data.get("encoding"),
        "content": content,
        "html_url": data.get("html_url"),
    }


def list_open_prs(
    owner: str,
    repo: str,
    state: str = "open",
    head: Optional[str] = None,
    base: Optional[str] = None,
    per_page: int = 30,
) -> List[Dict[str, Any]]:
    """List PRs for a repo, optionally filtered by state, head branch, or base branch."""
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/pulls"
    params: Dict[str, Any] = {"state": state, "per_page": per_page}
    if head:
        params["head"] = head
    if base:
        params["base"] = base
    data = _get(url, params=params).json()
    return [
        {
            "number": p.get("number"),
            "title": p.get("title"),
            "state": p.get("state"),
            "draft": p.get("draft"),
            "author": p.get("user", {}).get("login"),
            "base_branch": p.get("base", {}).get("ref"),
            "head_branch": p.get("head", {}).get("ref"),
            "head_sha": p.get("head", {}).get("sha"),
            "html_url": p.get("html_url"),
            "created_at": p.get("created_at"),
            "updated_at": p.get("updated_at"),
        }
        for p in data
    ]


# ---------------------------------------------------------------------------
# PR write functions (T2 — review posting)
# ---------------------------------------------------------------------------


def post_issue_comment(owner: str, repo: str, issue_number: int, body: str) -> Dict[str, Any]:
    """Post *body* as an issue comment on PR *issue_number*.

    Returns the parsed JSON response.  Raises ``requests.HTTPError`` on failure
    so callers can inspect ``response.status_code``.
    """
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/issues/{issue_number}/comments"
    resp = _post(url, {"body": body})
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
    """Attempt to post a formal review event on PR *pull_number*.

    Returns the raw ``requests.Response`` so callers can check the status code
    (201 success vs 422 self-review restriction) without raising.
    """
    payload: Dict[str, Any] = {"event": event, "body": body}
    if comments:
        payload["comments"] = comments
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
    return _post(url, payload)
