"""vcs_commit_files tool — commits files to a branch via vcs-service.

Thin passthrough to POST /api/vcs/repo/commit_files. Commits directly via
GitHub's Contents API — no local git clone involved. Use after
vcs_ensure_branch (if the branch is new) and before vcs_create_pr.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA: dict[str, Any] = {
    "description": (
        "Commit one or more files directly to a branch via vcs-service, "
        "using GitHub's Contents API (no local git clone involved). Use "
        "after vcs_ensure_branch (if the branch is new) and before "
        "vcs_create_pr, to give the PR's head branch actual content."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "owner": {
                "type": "string",
                "description": "GitHub org or user that owns the repo.",
            },
            "repo": {
                "type": "string",
                "description": "Repository name, without the owner prefix.",
            },
            "branch": {
                "type": "string",
                "description": "Branch to commit to.",
            },
            "message": {
                "type": "string",
                "description": "Commit message.",
            },
            "files": {
                "type": "object",
                "description": (
                    "Map of file path (relative to repo root) to full file "
                    "content. Each entry is created or overwritten wholesale — "
                    "not a diff/patch."
                ),
                "additionalProperties": {"type": "string"},
            },
            "base_branch": {
                "type": "string",
                "description": (
                    "Optional. If `branch` doesn't exist yet, create it from "
                    "this branch before committing."
                ),
            },
        },
        "required": ["owner", "repo", "branch", "message", "files"],
        "additionalProperties": False,
    },
}


def check_available(**_: Any) -> bool:
    from src.services.vcs_service_client import check_vcs_service_available

    return check_vcs_service_available()


def handle(
    owner: str = "",
    repo: str = "",
    branch: str = "",
    message: str = "",
    files: dict[str, str] | None = None,
    base_branch: str = "",
    **_: Any,
) -> dict[str, Any]:
    from src.services.vcs_service_client import VCSServiceError, commit_files, run_async

    if not owner:
        return {"ok": False, "error": "owner is required."}
    if not repo:
        return {"ok": False, "error": "repo is required."}
    if not branch:
        return {"ok": False, "error": "branch is required."}
    if not message:
        return {"ok": False, "error": "message is required."}
    if not files:
        return {"ok": False, "error": "files is required and must not be empty."}

    try:
        run_async(
            commit_files(
                owner.strip(),
                repo.strip(),
                branch.strip(),
                message.strip(),
                files,
                base_branch=base_branch.strip() if base_branch else "",
            )
        )
        return {"ok": True, "branch": branch.strip(), "files_committed": list(files.keys())}
    except VCSServiceError as exc:
        if exc.status and 400 <= exc.status < 500:
            return {"ok": False, "error": str(exc)}
        logger.warning("vcs_commit_files: backend error: %s", exc)
        return {"ok": False, "error": f"vcs-service request failed: {exc}"}
    except Exception as exc:
        logger.warning("vcs_commit_files: unexpected error: %s", exc)
        return {"ok": False, "error": f"vcs-service request failed: {exc}"}
