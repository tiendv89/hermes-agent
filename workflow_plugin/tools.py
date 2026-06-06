"""Tool schemas and handlers for the workflow plugin.

Read tools (v1):
    workflow_get_workspace_context  — reads workspace.yaml via workflow-backend
    workflow_get_feature_state      — reads feature status + artifact Markdown

Write tools (v2, T5):
    workflow_write_product_spec
    workflow_write_technical_design
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

import requests

from .client import WorkflowClient, _validate_id

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_GITHUB_API_URL = "https://api.github.com"

# Match SSH (git@github.com:owner/repo.git) and HTTPS GitHub URLs.
_GITHUB_SSH_RE = re.compile(r"git@github\.com:([^/]+)/([^\.]+?)(?:\.git)?$")
_GITHUB_HTTPS_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")


# ---------------------------------------------------------------------------
# JSON schemas
# ---------------------------------------------------------------------------

WS_CONTEXT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {
            "type": "string",
            "description": "The workspace identifier (from workspace.yaml).",
        },
    },
    "required": ["workspace_id"],
    "additionalProperties": False,
}

FEATURE_STATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {
            "type": "string",
            "description": "The workspace identifier.",
        },
        "feature_id": {
            "type": "string",
            "description": "The feature identifier (directory name under docs/features/).",
        },
    },
    "required": ["workspace_id", "feature_id"],
    "additionalProperties": False,
}

WRITE_SPEC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {
            "type": "string",
            "description": "The workspace identifier.",
        },
        "feature_id": {
            "type": "string",
            "description": "The feature identifier.",
        },
        "content": {
            "type": "string",
            "description": "Full Markdown content to write to product-spec.md.",
        },
        "commit_message": {
            "type": "string",
            "description": "Git commit message (optional, defaults to 'docs: update product spec').",
        },
    },
    "required": ["workspace_id", "feature_id", "content"],
    "additionalProperties": False,
}

WRITE_TD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {
            "type": "string",
            "description": "The workspace identifier.",
        },
        "feature_id": {
            "type": "string",
            "description": "The feature identifier.",
        },
        "content": {
            "type": "string",
            "description": "Full Markdown content to write to technical-design.md.",
        },
        "commit_message": {
            "type": "string",
            "description": "Git commit message (optional, defaults to 'docs: update technical design').",
        },
    },
    "required": ["workspace_id", "feature_id", "content"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Availability guard
# ---------------------------------------------------------------------------

def check_workflow_available(**_kwargs: Any) -> bool:
    """Return True only when WORKFLOW_BACKEND_URL is configured."""
    return bool(os.environ.get("WORKFLOW_BACKEND_URL", "").strip())


# ---------------------------------------------------------------------------
# GitHub helpers
# ---------------------------------------------------------------------------

def _parse_github_owner_repo(github_url: str) -> Tuple[str, str]:
    """Parse (owner, repo) from a GitHub SSH or HTTPS URL.

    Supports:
        git@github.com:owner/repo.git
        https://github.com/owner/repo.git
        https://github.com/owner/repo
    """
    m = _GITHUB_SSH_RE.match(github_url.strip())
    if m:
        return m.group(1), m.group(2)
    m = _GITHUB_HTTPS_RE.match(github_url.strip())
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"Cannot parse GitHub owner/repo from URL: {github_url!r}")


def _get_management_repo_github(workspace_context: Dict[str, Any]) -> Tuple[str, str]:
    """Extract (owner, repo) for the management repo from a workspace context dict.

    The context is the JSON payload returned by
    ``GET /api/workspaces/{workspace_id}`` on the workflow-backend.

    Expected shape::

        {
          "management_repo": "<repo_id>",
          "repos": [
            { "id": "<repo_id>", "github": "git@github.com:owner/repo.git", ... },
            ...
          ]
        }
    """
    management_repo_id: Optional[str] = workspace_context.get("management_repo")
    repos: list = workspace_context.get("repos", [])

    # Prefer the repo whose id matches management_repo.
    if management_repo_id:
        for repo in repos:
            if isinstance(repo, dict) and repo.get("id") == management_repo_id:
                github_url = repo.get("github", "")
                if github_url:
                    return _parse_github_owner_repo(github_url)

    # Fallback: first repo with "management" in the id.
    for repo in repos:
        if isinstance(repo, dict) and "management" in repo.get("id", ""):
            github_url = repo.get("github", "")
            if github_url:
                return _parse_github_owner_repo(github_url)

    raise ValueError(
        "Could not resolve management repo GitHub URL from workspace context. "
        f"management_repo={management_repo_id!r}, repos={repos!r}"
    )


def _github_get_file_sha(
    owner: str,
    repo: str,
    path: str,
    token: str,
) -> Optional[str]:
    """Return the blob SHA of an existing file, or None if the file doesn't exist."""
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }
    resp = requests.get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("sha")


def _github_put_file(
    owner: str,
    repo: str,
    path: str,
    content: str,
    commit_message: str,
    token: str,
) -> Dict[str, Any]:
    """Create or update a file via the GitHub Contents API.

    Fetches the current blob SHA first so that updates to existing files
    succeed (the API requires ``sha`` when the file already exists).
    """
    existing_sha = _github_get_file_sha(owner, repo, path, token)
    content_b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")

    payload: Dict[str, Any] = {
        "message": commit_message,
        "content": content_b64,
    }
    if existing_sha is not None:
        payload["sha"] = existing_sha

    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }
    resp = requests.put(url, headers=headers, json=payload, timeout=_DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _write_artifact(
    workspace_id: str,
    feature_id: str,
    filename: str,
    content: str,
    commit_message: str,
) -> Dict[str, Any]:
    """Shared logic for both write-artifact handlers.

    1. Validate feature_id to prevent path traversal.
    2. Verify GITHUB_TOKEN is set.
    3. Fetch workspace context to resolve management-repo owner/repo.
    4. PUT the file via the GitHub Contents API.
    """
    _validate_id(feature_id, "feature_id")

    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    client = WorkflowClient()
    workspace_context = client.get_workspace_context(workspace_id)
    owner, repo = _get_management_repo_github(workspace_context)

    path = f"docs/features/{feature_id}/{filename}"
    result = _github_put_file(owner, repo, path, content, commit_message, github_token)

    commit_sha: str = result.get("commit", {}).get("sha", "")
    return {"ok": True, "path": path, "commit": commit_sha}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_get_workspace_context(workspace_id: str, **_kwargs: Any) -> Dict[str, Any]:
    """Return workspace metadata (repos, roles, model_policy) from workflow-backend."""
    try:
        client = WorkflowClient()
        data = client.get_workspace_context(workspace_id)
        return {"ok": True, "workspace": data}
    except Exception as exc:
        logger.warning("workflow_get_workspace_context failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def handle_get_feature_state(
    workspace_id: str,
    feature_id: str,
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Return feature lifecycle state plus available artifact excerpts."""
    try:
        client = WorkflowClient()
        detail = client.get_feature_detail(workspace_id, feature_id)
        return {"ok": True, "feature": detail}
    except Exception as exc:
        logger.warning("workflow_get_feature_state failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def handle_write_product_spec(
    workspace_id: str,
    feature_id: str,
    content: str,
    commit_message: str = "docs: update product spec",
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Write product-spec.md to the management repo via the GitHub Contents API."""
    try:
        return _write_artifact(
            workspace_id=workspace_id,
            feature_id=feature_id,
            filename="product-spec.md",
            content=content,
            commit_message=commit_message,
        )
    except Exception as exc:
        logger.warning("workflow_write_product_spec failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def handle_write_technical_design(
    workspace_id: str,
    feature_id: str,
    content: str,
    commit_message: str = "docs: update technical design",
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Write technical-design.md to the management repo via the GitHub Contents API."""
    try:
        return _write_artifact(
            workspace_id=workspace_id,
            feature_id=feature_id,
            filename="technical-design.md",
            content=content,
            commit_message=commit_message,
        )
    except Exception as exc:
        logger.warning("workflow_write_technical_design failed: %s", exc)
        return {"ok": False, "error": str(exc)}
