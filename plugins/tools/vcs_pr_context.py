"""vcs_pr_context tool — read-only GitHub PR context via vcs-service (G7).

Single tool with an ``action`` enum selector, matching the existing
``gitnexus.py``/``rag.py`` convention:

    diff          → unified diff for the PR
    files         → changed-file list (paths, additions/deletions, status)
    metadata      → title, body, author, branches, state, labels, etc.
    comments      → issue-level + inline review comments
    reviews       → review history (who, verdict, when)
    checks        → CI check-run results for the PR's head SHA (bounded poll)
    commits       → commit history for the PR
    compare       → ahead/behind counts + diff between two refs/branches/commits
    file_at_ref   → full file content at a given ref/commit
    list_prs      → list open (or filtered) PRs for a repo

Proxies through vcs-service (no GITHUB_TOKEN / local GitHub PAT needed —
vcs-service resolves a scoped GitHub App installation token per-owner
internally). Gated on VCS_SERVICE_URL/VCS_SERVICE_TOKEN presence.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_PR_URL_RE_SOURCE = r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)"

_ACTIONS = (
    "diff",
    "files",
    "metadata",
    "comments",
    "reviews",
    "checks",
    "commits",
    "compare",
    "file_at_ref",
    "list_prs",
)

_PR_ACTIONS = {"diff", "files", "metadata", "comments", "reviews", "checks", "commits"}

SCHEMA: Dict[str, Any] = {
    "description": (
        "Read-only GitHub PR context — fetch diff, files, metadata, comments, reviews, "
        "CI check-runs, commits, ref comparison, file content at a ref, or list open PRs. "
        "Use this before reviewing a PR, answering 'what changed', 'has anyone commented', "
        "'why did CI fail', or 'what does this function currently look like'. "
        "PR-scoped actions (diff/files/metadata/comments/reviews/checks/commits) require pr_url. "
        "compare requires owner, repo, base, and head. "
        "file_at_ref requires owner, repo, path, and ref. "
        "list_prs requires owner and repo."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": list(_ACTIONS),
                "description": (
                    "Operation to perform: "
                    "diff (unified diff text), "
                    "files (changed-file list), "
                    "metadata (title/body/author/state/labels/branches), "
                    "comments (issue-level + inline review comments), "
                    "reviews (review history — who, verdict, when), "
                    "checks (CI check-run results; polls up to CHAT_REVIEW_CI_POLL_TIMEOUT_SECONDS), "
                    "commits (PR commit history), "
                    "compare (ahead/behind + diff between two refs), "
                    "file_at_ref (full file content at a given ref), "
                    "list_prs (open/filtered PRs for a repo)."
                ),
            },
            "pr_url": {
                "type": "string",
                "description": (
                    "GitHub PR URL: https://github.com/{owner}/{repo}/pull/{number}. "
                    "Required for: diff, files, metadata, comments, reviews, checks, commits."
                ),
            },
            "owner": {
                "type": "string",
                "description": (
                    "GitHub repository owner (user or org). "
                    "Required for: compare, file_at_ref, list_prs. "
                    "Inferred from pr_url for PR-scoped actions if omitted."
                ),
            },
            "repo": {
                "type": "string",
                "description": (
                    "GitHub repository name (without owner). "
                    "Required for: compare, file_at_ref, list_prs. "
                    "Inferred from pr_url for PR-scoped actions if omitted."
                ),
            },
            "base": {
                "type": "string",
                "description": "Base ref/branch/SHA for action='compare'.",
            },
            "head": {
                "type": "string",
                "description": (
                    "Head ref/branch/SHA for action='compare'. "
                    "Also used as an optional branch filter for action='list_prs'."
                ),
            },
            "path": {
                "type": "string",
                "description": "File path within the repo. Required for action='file_at_ref'.",
            },
            "ref": {
                "type": "string",
                "description": (
                    "Git ref (branch, tag, or commit SHA) to read the file at. "
                    "Required for action='file_at_ref'."
                ),
            },
            "state": {
                "type": "string",
                "enum": ["open", "closed", "all"],
                "default": "open",
                "description": "PR state filter for action='list_prs'. Default: open.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def check_available(**_: Any) -> bool:
    from src.services.vcs_service_client import check_vcs_service_available

    return check_vcs_service_available()


def _parse_pr_url(pr_url: str) -> Tuple[str, str, int]:
    import re

    m = re.match(_PR_URL_RE_SOURCE, pr_url.strip())
    if not m:
        raise ValueError(
            f"Invalid GitHub PR URL {pr_url!r}. "
            "Expected https://github.com/{{owner}}/{{repo}}/pull/{{number}}."
        )
    return m.group(1), m.group(2), int(m.group(3))


def handle(
    action: str = "",
    pr_url: str = "",
    owner: str = "",
    repo: str = "",
    base: str = "",
    head: str = "",
    path: str = "",
    ref: str = "",
    state: str = "open",
    **_: Any,
) -> Dict[str, Any]:
    from src.services.vcs_service_client import VCSServiceError, run_async
    from src.services import vcs_service_client as vcs

    if action not in _ACTIONS:
        return {
            "ok": False,
            "error": f"Unknown action {action!r}. Expected one of: {', '.join(_ACTIONS)}.",
        }

    parsed_owner: str = owner
    parsed_repo: str = repo
    pull_number: Optional[int] = None

    if action in _PR_ACTIONS:
        if not pr_url:
            return {"ok": False, "error": f"pr_url is required for action={action!r}."}
        try:
            parsed_owner, parsed_repo, pull_number = _parse_pr_url(pr_url)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    try:
        if action == "diff":
            result = run_async(vcs.get_pr_diff(parsed_owner, parsed_repo, pull_number))
            return {"ok": True, "diff": result}

        if action == "files":
            data = run_async(vcs.get_pr_files(parsed_owner, parsed_repo, pull_number))
            return {"ok": True, **data}

        if action == "metadata":
            data = run_async(vcs.get_pr_metadata(parsed_owner, parsed_repo, pull_number))
            return {"ok": True, "metadata": data}

        if action == "comments":
            data = run_async(vcs.get_pr_comments(parsed_owner, parsed_repo, pull_number))
            return {"ok": True, **data}

        if action == "reviews":
            data = run_async(vcs.get_pr_reviews(parsed_owner, parsed_repo, pull_number))
            return {"ok": True, **data}

        if action == "checks":
            meta = run_async(vcs.get_pr_metadata(parsed_owner, parsed_repo, pull_number))
            head_sha = meta.get("head_sha", "")
            if not head_sha:
                return {"ok": False, "error": "Could not determine head SHA from PR metadata."}
            poll_timeout = int(os.environ.get("CHAT_REVIEW_CI_POLL_TIMEOUT_SECONDS", "60"))
            data = run_async(
                vcs.get_check_runs(
                    parsed_owner, parsed_repo, head_sha, poll_timeout_seconds=poll_timeout
                )
            )
            return {"ok": True, **data}

        if action == "commits":
            data = run_async(vcs.get_pr_commits(parsed_owner, parsed_repo, pull_number))
            return {"ok": True, **data}

        if action == "compare":
            if not parsed_owner or not parsed_repo:
                return {"ok": False, "error": "owner and repo are required for action='compare'."}
            if not base or not head:
                return {"ok": False, "error": "base and head are required for action='compare'."}
            data = run_async(vcs.compare_refs(parsed_owner, parsed_repo, base, head))
            return {"ok": True, **data}

        if action == "file_at_ref":
            if not parsed_owner or not parsed_repo:
                return {
                    "ok": False,
                    "error": "owner and repo are required for action='file_at_ref'.",
                }
            if not path:
                return {"ok": False, "error": "path is required for action='file_at_ref'."}
            if not ref:
                return {"ok": False, "error": "ref is required for action='file_at_ref'."}
            data = run_async(vcs.get_file_at_ref(parsed_owner, parsed_repo, path, ref))
            return {"ok": True, **data}

        if action == "list_prs":
            if not parsed_owner or not parsed_repo:
                return {"ok": False, "error": "owner and repo are required for action='list_prs'."}
            data = run_async(
                vcs.list_prs(parsed_owner, parsed_repo, state=state or "open", head=head or "")
            )
            return {"ok": True, "pull_requests": data.get("prs", [])}

    except VCSServiceError as exc:
        logger.warning("vcs_pr_context action=%r vcs-service error: %s", action, exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("vcs_pr_context action=%r failed: %s", action, exc)
        return {"ok": False, "error": str(exc)}

    return {"ok": False, "error": f"Unhandled action {action!r}."}
