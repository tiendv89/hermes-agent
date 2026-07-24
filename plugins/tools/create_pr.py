"""vcs_create_pr tool — creates a pull request via vcs-service.

Thin passthrough to POST /api/vcs/pr/create: vcs-service resolves a scoped
GitHub App installation token per-owner internally, so this tool needs no
GITHUB_TOKEN — only VCS_SERVICE_URL / VCS_SERVICE_TOKEN.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA: dict[str, Any] = {
    "description": (
        "Create a pull request via vcs-service. Opens a PR from `head` into "
        "`base` on the given GitHub repo. Returns the PR number and html_url "
        "on success."
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
            "title": {
                "type": "string",
                "description": "Pull request title.",
            },
            "head": {
                "type": "string",
                "description": "Branch containing the changes (source branch).",
            },
            "base": {
                "type": "string",
                "description": "Branch the PR merges into (target branch).",
            },
            "body": {
                "type": "string",
                "description": "Optional PR description.",
            },
            "draft": {
                "type": "boolean",
                "description": "Optional. Create as a draft PR. Defaults to false.",
            },
        },
        "required": ["owner", "repo", "title", "head", "base"],
        "additionalProperties": False,
    },
}


def check_available(**_: Any) -> bool:
    from src.services.vcs_service_client import check_vcs_service_available

    return check_vcs_service_available()


def handle(
    owner: str = "",
    repo: str = "",
    title: str = "",
    head: str = "",
    base: str = "",
    body: str = "",
    draft: bool = False,
    **_: Any,
) -> dict[str, Any]:
    from src.services.vcs_service_client import VCSServiceError, create_pr, run_async

    if not owner:
        return {"ok": False, "error": "owner is required."}
    if not repo:
        return {"ok": False, "error": "repo is required."}
    if not title:
        return {"ok": False, "error": "title is required."}
    if not head:
        return {"ok": False, "error": "head is required."}
    if not base:
        return {"ok": False, "error": "base is required."}

    try:
        data = run_async(
            create_pr(
                owner.strip(),
                repo.strip(),
                title.strip(),
                head.strip(),
                base.strip(),
                body=body.strip() if body else "",
                draft=bool(draft),
            )
        )
        return {
            "ok": True,
            "number": data.get("number"),
            "html_url": data.get("html_url"),
            "state": data.get("state"),
            "head_ref": data.get("head_ref"),
            "base_ref": data.get("base_ref"),
        }
    except VCSServiceError as exc:
        if exc.status and 400 <= exc.status < 500:
            return {"ok": False, "error": str(exc)}
        logger.warning("vcs_create_pr: backend error: %s", exc)
        return {"ok": False, "error": f"vcs-service request failed: {exc}"}
    except Exception as exc:
        logger.warning("vcs_create_pr: unexpected error: %s", exc)
        return {"ok": False, "error": f"vcs-service request failed: {exc}"}
