"""vcs_ensure_branch tool — creates a branch from a base branch via vcs-service.

Thin passthrough to POST /api/vcs/repo/ensure_branch. Use before
vcs_create_pr when the PR's head branch doesn't exist on the remote yet —
GitHub's PR API 422s if `head` isn't already pushed.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "description": (
        "Create a branch from a base branch via vcs-service, if it doesn't "
        "already exist on the remote. Use this before vcs_create_pr when the "
        "PR's head branch hasn't been pushed yet — GitHub's PR API rejects "
        "opening a PR whose head branch doesn't exist."
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
                "description": "Name of the branch to create.",
            },
            "base_branch": {
                "type": "string",
                "description": "Existing branch to branch from (e.g. main).",
            },
        },
        "required": ["owner", "repo", "branch", "base_branch"],
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
    base_branch: str = "",
    **_: Any,
) -> Dict[str, Any]:
    from src.services.vcs_service_client import VCSServiceError, ensure_branch, run_async

    if not owner:
        return {"ok": False, "error": "owner is required."}
    if not repo:
        return {"ok": False, "error": "repo is required."}
    if not branch:
        return {"ok": False, "error": "branch is required."}
    if not base_branch:
        return {"ok": False, "error": "base_branch is required."}

    try:
        run_async(
            ensure_branch(owner.strip(), repo.strip(), branch.strip(), base_branch.strip())
        )
        return {"ok": True, "branch": branch.strip()}
    except VCSServiceError as exc:
        if exc.status and 400 <= exc.status < 500:
            return {"ok": False, "error": str(exc)}
        logger.warning("vcs_ensure_branch: backend error: %s", exc)
        return {"ok": False, "error": f"vcs-service request failed: {exc}"}
    except Exception as exc:
        logger.warning("vcs_ensure_branch: unexpected error: %s", exc)
        return {"ok": False, "error": f"vcs-service request failed: {exc}"}
