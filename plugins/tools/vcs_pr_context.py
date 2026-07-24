"""github_pr_context tool — read-only GitHub PR context (G7).

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

Gated on ``VCS_SERVICE_URL`` presence (vcs-service must be reachable).
All operations route through vcs-service proxy endpoints — no direct
GitHub API calls.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from plugins.clients.vcs_client import (
    compare_refs,
    get_check_runs,
    get_file_at_ref,
    get_pr_comments,
    get_pr_commits,
    get_pr_diff,
    get_pr_files,
    get_pr_metadata,
    get_pr_reviews,
    list_open_prs,
    parse_pr_url,
)

logger = logging.getLogger(__name__)

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

SCHEMA: dict[str, Any] = {
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
    """Return True only when VCS_SERVICE_URL is configured."""
    return bool(os.environ.get("VCS_SERVICE_URL", "").strip())


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
) -> dict[str, Any]:
    if action not in _ACTIONS:
        return {
            "ok": False,
            "error": f"Unknown action {action!r}. Expected one of: {', '.join(_ACTIONS)}.",
        }

    if not os.environ.get("VCS_SERVICE_URL", "").strip():
        return {"ok": False, "error": "VCS_SERVICE_URL is not configured."}

    # PR-scoped actions require pr_url; owner/repo are inferred from it.
    _PR_ACTIONS = {
        "diff",
        "files",
        "metadata",
        "comments",
        "reviews",
        "checks",
        "commits",
    }

    parsed_owner: str = owner
    parsed_repo: str = repo
    pull_number: int | None = None

    if action in _PR_ACTIONS:
        if not pr_url:
            return {
                "ok": False,
                "error": f"pr_url is required for action={action!r}.",
            }
        try:
            parsed_owner, parsed_repo, pull_number = parse_pr_url(pr_url)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    try:
        if action == "diff":
            result = get_pr_diff(parsed_owner, parsed_repo, pull_number)
            return {"ok": True, "diff": result}

        if action == "files":
            result = get_pr_files(parsed_owner, parsed_repo, pull_number)
            return {"ok": True, "files": result}

        if action == "metadata":
            result = get_pr_metadata(parsed_owner, parsed_repo, pull_number)
            return {"ok": True, "metadata": result}

        if action == "comments":
            result = get_pr_comments(parsed_owner, parsed_repo, pull_number)
            return {"ok": True, **result}

        if action == "reviews":
            result = get_pr_reviews(parsed_owner, parsed_repo, pull_number)
            return {"ok": True, "reviews": result}

        if action == "checks":
            # Resolve head SHA from PR metadata when not provided directly.
            meta = get_pr_metadata(parsed_owner, parsed_repo, pull_number)
            head_sha = meta.get("head_sha", "")
            if not head_sha:
                return {
                    "ok": False,
                    "error": "Could not determine head SHA from PR metadata.",
                }
            result = get_check_runs(parsed_owner, parsed_repo, head_sha)
            return {"ok": True, **result}

        if action == "commits":
            result = get_pr_commits(parsed_owner, parsed_repo, pull_number)
            return {"ok": True, "commits": result}

        if action == "compare":
            if not parsed_owner or not parsed_repo:
                return {
                    "ok": False,
                    "error": "owner and repo are required for action='compare'.",
                }
            if not base or not head:
                return {
                    "ok": False,
                    "error": "base and head are required for action='compare'.",
                }
            result = compare_refs(parsed_owner, parsed_repo, base, head)
            return {"ok": True, **result}

        if action == "file_at_ref":
            if not parsed_owner or not parsed_repo:
                return {
                    "ok": False,
                    "error": "owner and repo are required for action='file_at_ref'.",
                }
            if not path:
                return {
                    "ok": False,
                    "error": "path is required for action='file_at_ref'.",
                }
            if not ref:
                return {
                    "ok": False,
                    "error": "ref is required for action='file_at_ref'.",
                }
            result = get_file_at_ref(parsed_owner, parsed_repo, path, ref)
            if not result.get("ok", True):
                return {"ok": False, "error": result.get("error", "Unknown error.")}
            return {"ok": True, **result}

        if action == "list_prs":
            if not parsed_owner or not parsed_repo:
                return {
                    "ok": False,
                    "error": "owner and repo are required for action='list_prs'.",
                }
            result = list_open_prs(
                parsed_owner,
                parsed_repo,
                state=state or "open",
                head=head or None,
            )
            return {"ok": True, "pull_requests": result}

    except Exception as exc:
        logger.warning("github_pr_context action=%r failed: %s", action, exc)
        return {"ok": False, "error": str(exc)}

    return {"ok": False, "error": f"Unhandled action {action!r}."}
