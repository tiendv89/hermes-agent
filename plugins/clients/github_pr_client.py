"""Shared GitHub PR client for hermes-agent.

Implements the same function signatures as before but now delegates all VCS
operations to ``plugins.clients.vcs_client``, which routes through the
vcs-service proxy endpoints instead of calling GitHub directly.

``parse_pr_url`` remains a pure regex utility with no external dependencies.

Exported functions are consumed by ``plugins/tools/github_pr_context.py`` and
``plugins/tools/github_pr_review.py`` when those tools haven't yet migrated
to importing from ``vcs_client`` directly.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

_GITHUB_API_URL = "https://api.github.com"
_DEFAULT_TIMEOUT = 30

_PR_URL_RE = re.compile(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)")


# ---------------------------------------------------------------------------
# Internal helpers — delegate to vcs_client where possible
# ---------------------------------------------------------------------------


def _token() -> str:
    """Token resolution is handled by vcs-service — consumers should not need this."""
    return ""


def _headers(accept: str = "application/vnd.github.v3+json") -> Dict[str, str]:
    """Auth is handled by vcs-service — returns minimal headers for direct use."""
    return {"Accept": accept}


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
# PR-level read functions — delegate to vcs_client
# ---------------------------------------------------------------------------


def get_pr_metadata(owner: str, repo: str, pull_number: int) -> Dict[str, Any]:
    """Return PR metadata: title, body, author, branches, state, labels, etc."""
    from plugins.clients.vcs_client import get_pr_metadata as _impl

    return _impl(owner, repo, pull_number)


def get_pr_diff(owner: str, repo: str, pull_number: int) -> str:
    """Return the unified diff for the PR as a string."""
    from plugins.clients.vcs_client import get_pr_diff as _impl

    return _impl(owner, repo, pull_number)


def get_pr_files(owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
    """Return the list of files changed in the PR."""
    from plugins.clients.vcs_client import get_pr_files as _impl

    return _impl(owner, repo, pull_number)


def get_pr_comments(owner: str, repo: str, pull_number: int) -> Dict[str, Any]:
    """Return both issue-level and review-level (inline) comments on the PR."""
    from plugins.clients.vcs_client import get_pr_comments as _impl

    return _impl(owner, repo, pull_number)


def get_pr_reviews(owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
    """Return review history for the PR (who reviewed, verdict, when)."""
    from plugins.clients.vcs_client import get_pr_reviews as _impl

    return _impl(owner, repo, pull_number)


def get_pr_commits(owner: str, repo: str, pull_number: int) -> List[Dict[str, Any]]:
    """Return the commits in the PR."""
    from plugins.clients.vcs_client import get_pr_commits as _impl

    return _impl(owner, repo, pull_number)


def get_check_runs(
    owner: str,
    repo: str,
    head_sha: str,
    poll_timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    """Return CI check-run results for *head_sha*, with an optional bounded poll."""
    from plugins.clients.vcs_client import get_check_runs as _impl

    return _impl(owner, repo, head_sha, poll_timeout_seconds)


# ---------------------------------------------------------------------------
# Commit-level read functions — delegate to vcs_client
# ---------------------------------------------------------------------------


def compare_refs(
    owner: str,
    repo: str,
    base: str,
    head: str,
) -> Dict[str, Any]:
    """Compare two refs/branches/commits and return ahead/behind counts + diff."""
    from plugins.clients.vcs_client import compare_refs as _impl

    return _impl(owner, repo, base, head)


# ---------------------------------------------------------------------------
# Repo/file-level read functions — delegate to vcs_client
# ---------------------------------------------------------------------------


def get_file_at_ref(
    owner: str,
    repo: str,
    path: str,
    ref: str,
) -> Dict[str, Any]:
    """Return the content of a file at a given ref/commit/branch."""
    from plugins.clients.vcs_client import get_file_at_ref as _impl

    return _impl(owner, repo, path, ref)


def list_open_prs(
    owner: str,
    repo: str,
    state: str = "open",
    head: Optional[str] = None,
    base: Optional[str] = None,
    per_page: int = 30,
) -> List[Dict[str, Any]]:
    """List PRs for a repo, optionally filtered by state, head branch, or base branch."""
    from plugins.clients.vcs_client import list_open_prs as _impl

    return _impl(owner, repo, state=state, head=head)


# ---------------------------------------------------------------------------
# PR write functions — delegate to vcs_client
# ---------------------------------------------------------------------------


def post_issue_comment(
    owner: str, repo: str, issue_number: int, body: str
) -> Dict[str, Any]:
    """Post *body* as an issue comment on PR *issue_number*.

    Returns the parsed JSON response.  Raises ``requests.HTTPError`` on failure.
    """
    from plugins.clients.vcs_client import post_issue_comment as _impl

    return _impl(owner, repo, issue_number, body)


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
    from plugins.clients.vcs_client import post_pr_review as _impl

    return _impl(owner, repo, pull_number, event, body, comments)
