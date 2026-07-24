"""Coding-profile git-operation tools — deferred execution.

Every handler returns a ``{"__deferred__": True, "tool": "...", "params": {...}}``
marker.  The IDE extension executes the git operation using the developer's
local git state (SSH keys, config, remotes) and returns the result.

Tools
-----
* ``git_status``   — working-tree status (modified, staged, untracked)
* ``git_diff``     — diff of uncommitted changes
* ``git_commit``   — commit staged changes with a message
* ``git_push``     — push commits to the remote
* ``git_checkout`` — switch branches (or create a new one)
* ``git_log``      — commit history
"""

from __future__ import annotations

from typing import Any

from plugins.tools.deferred import deferred

# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

GIT_STATUS_SCHEMA: dict[str, Any] = {
    "description": (
        "Get the working-tree status from the developer's local git repository. "
        "Returns lists of modified, staged, and untracked files, plus the "
        "current branch name."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    },
}

GIT_DIFF_SCHEMA: dict[str, Any] = {
    "description": (
        "Get the unified diff of uncommitted changes (unstaged, staged, or both) "
        "from the developer's local git repository."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "staged": {
                "type": "boolean",
                "description": "If true, show staged changes only. "
                "If false (default), show unstaged changes. "
                "Omit entirely to show both.",
            },
            "path": {
                "type": "string",
                "description": "Optional file or directory path to restrict the diff to.",
            },
        },
        "additionalProperties": False,
    },
}

GIT_COMMIT_SCHEMA: dict[str, Any] = {
    "description": (
        "Commit staged changes in the developer's local git repository. "
        "ONLY commits files that are already staged — use after git add. "
        "Returns the commit hash."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "Commit message (required).",
            },
        },
        "required": ["message"],
        "additionalProperties": False,
    },
}

GIT_PUSH_SCHEMA: dict[str, Any] = {
    "description": (
        "Push committed changes from the developer's local repository to the "
        "remote. Pushes the current branch by default."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "remote": {
                "type": "string",
                "description": "Remote name (defaults to 'origin').",
            },
            "branch": {
                "type": "string",
                "description": "Branch to push (defaults to the current branch).",
            },
            "set_upstream": {
                "type": "boolean",
                "description": "Set the upstream tracking reference (default: false).",
            },
        },
        "additionalProperties": False,
    },
}

GIT_CHECKOUT_SCHEMA: dict[str, Any] = {
    "description": (
        "Switch to a branch (or create a new one with -b) in the developer's "
        "local git repository."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "branch": {
                "type": "string",
                "description": "Branch name to switch to (required).",
            },
            "create": {
                "type": "boolean",
                "description": "If true, create the branch before switching.",
            },
        },
        "required": ["branch"],
        "additionalProperties": False,
    },
}

GIT_LOG_SCHEMA: dict[str, Any] = {
    "description": (
        "Show the commit history from the developer's local git repository."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "count": {
                "type": "integer",
                "description": "Number of recent commits to show (default: 10).",
            },
            "branch": {
                "type": "string",
                "description": "Branch to show history for (defaults to current branch).",
            },
        },
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def handle_git_status(**_kwargs: Any) -> dict[str, Any]:
    """Defer execution — the IDE extension runs git status locally."""
    return deferred("git_status", {})


def handle_git_diff(
    staged: bool | None = None,
    path: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension runs git diff locally."""
    params: dict[str, Any] = {}
    if staged is not None:
        params["staged"] = staged
    if path:
        params["path"] = path
    return deferred("git_diff", params)


def handle_git_commit(
    message: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension commits staged changes."""
    if not message:
        return {"ok": False, "error": "message is required"}
    return deferred("git_commit", {"message": message})


def handle_git_push(
    remote: str = "",
    branch: str = "",
    set_upstream: bool = False,
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension pushes commits to the remote."""
    params: dict[str, Any] = {}
    if remote:
        params["remote"] = remote
    if branch:
        params["branch"] = branch
    if set_upstream:
        params["set_upstream"] = set_upstream
    return deferred("git_push", params)


def handle_git_checkout(
    branch: str = "",
    create: bool = False,
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension switches/creates a branch."""
    if not branch:
        return {"ok": False, "error": "branch is required"}
    params: dict[str, Any] = {"branch": branch}
    if create:
        params["create"] = create
    return deferred("git_checkout", params)


def handle_git_log(
    count: int | None = None,
    branch: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension shows commit history."""
    params: dict[str, Any] = {}
    if count is not None:
        params["count"] = count
    if branch:
        params["branch"] = branch
    return deferred("git_log", params)
