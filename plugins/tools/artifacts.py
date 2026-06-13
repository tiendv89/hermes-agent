"""workflow_write_product_spec / workflow_write_technical_design tools.

Full-rewrite handlers: read the current document SHA, replace the entire
content, and commit to the feature branch via the document_repo pipeline.
This replaces the old direct-to-main Contents PUT and fixes the
"no direct push to main" rule violation.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional, Tuple


from ..db import _validate_id, get_workspace_context
from ..document_repo import StaleBaseError, read_document, write_document

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_GITHUB_API_URL = "https://api.github.com"
_GITHUB_SSH_RE = re.compile(r"git@github\.com:([^/]+)/([^\.]+?)(?:\.git)?$")
_GITHUB_HTTPS_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")

WRITE_SPEC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {"type": "string", "description": "Workspace identifier. Omit to use the current workspace from context."},
        "feature_id": {"type": "string", "description": "Feature identifier. Omit to use the current feature from context."},
        "content": {"type": "string", "description": "Full Markdown content to write to product-spec.md."},
        "commit_message": {"type": "string", "description": "Git commit message (optional)."},
    },
    "required": ["content"],
    "additionalProperties": False,
}

WRITE_TD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {"type": "string", "description": "Workspace identifier. Omit to use the current workspace from context."},
        "feature_id": {"type": "string", "description": "Feature identifier. Omit to use the current feature from context."},
        "content": {"type": "string", "description": "Full Markdown content to write to technical-design.md."},
        "commit_message": {"type": "string", "description": "Git commit message (optional)."},
    },
    "required": ["content"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# GitHub helpers — kept for backward compat and use in document_repo resolution
# ---------------------------------------------------------------------------

def _parse_github_owner_repo(github_url: str) -> Tuple[str, str]:
    m = _GITHUB_SSH_RE.match(github_url.strip()) or _GITHUB_HTTPS_RE.match(github_url.strip())
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"Cannot parse GitHub owner/repo from URL: {github_url!r}")


def _resolve_management_repo(workspace_context: Dict[str, Any]) -> Tuple[str, str]:
    management_repo_id: Optional[str] = workspace_context.get("management_repo")
    repos: list = workspace_context.get("repos", [])

    if management_repo_id:
        for repo in repos:
            if isinstance(repo, dict) and repo.get("id") == management_repo_id:
                if repo.get("github"):
                    return _parse_github_owner_repo(repo["github"])

    for repo in repos:
        if isinstance(repo, dict) and "management" in repo.get("id", "") and repo.get("github"):
            return _parse_github_owner_repo(repo["github"])

    raise ValueError(
        f"Could not resolve management repo from workspace context. "
        f"management_repo={management_repo_id!r}, repos={repos!r}"
    )


# ---------------------------------------------------------------------------
# Internal write pipeline
# ---------------------------------------------------------------------------

def _write_artifact(
    workspace_id: str,
    feature_id: str,
    filename: str,
    content: str,
    commit_message: str,
) -> Dict[str, Any]:
    """Full-rewrite path: read current SHA then write to feature/<feature_id>."""
    _validate_id(feature_id, "feature_id")
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    workspace_context = get_workspace_context(workspace_id)
    owner, repo = _resolve_management_repo(workspace_context)
    base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")
    branch = f"feature/{feature_id}"
    path = f"docs/features/{feature_id}/{filename}"

    # Read-before-write: fetch the current SHA so GitHub accepts our PUT.
    current = read_document(owner, repo, branch, path, github_token)

    result = write_document(
        owner, repo, feature_id, base_branch, path, content, current["sha"], commit_message, github_token
    )
    return {
        "ok": True,
        "path": path,
        "commit": result["commit_sha"],
        "commit_sha": result["commit_sha"],
        "pr_url": result["pr"].get("url", ""),
        "conflict": False,
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _resolve_ids(workspace_id: str, feature_id: str) -> tuple[str, str]:
    from ..context import get_feature_id, get_workspace_id

    return workspace_id or get_workspace_id(), feature_id or get_feature_id()


def handle_write_product_spec(
    content: str,
    workspace_id: str = "",
    feature_id: str = "",
    commit_message: str = "docs: update product spec",
    **_: Any,
) -> Dict[str, Any]:
    wid, fid = _resolve_ids(workspace_id, feature_id)
    if not wid or not fid:
        return {"ok": False, "error": "workspace_id and feature_id are required but were not provided and no context is set."}
    try:
        return _write_artifact(wid, fid, "product-spec.md", content, commit_message)
    except StaleBaseError as exc:
        return {"ok": False, "conflict": True, "error": str(exc)}
    except Exception as exc:
        logger.warning("workflow_write_product_spec failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def handle_write_technical_design(
    content: str,
    workspace_id: str = "",
    feature_id: str = "",
    commit_message: str = "docs: update technical design",
    **_: Any,
) -> Dict[str, Any]:
    wid, fid = _resolve_ids(workspace_id, feature_id)
    if not wid or not fid:
        return {"ok": False, "error": "workspace_id and feature_id are required but were not provided and no context is set."}
    try:
        return _write_artifact(wid, fid, "technical-design.md", content, commit_message)
    except StaleBaseError as exc:
        return {"ok": False, "conflict": True, "error": str(exc)}
    except Exception as exc:
        logger.warning("workflow_write_technical_design failed: %s", exc)
        return {"ok": False, "error": str(exc)}
