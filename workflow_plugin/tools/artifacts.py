"""workflow_write_product_spec / workflow_write_technical_design tools.

Writes Markdown artifacts directly to the management repo via the GitHub
Contents API. Requires GITHUB_TOKEN in the environment.
"""

from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

import requests

from ..db import _validate_id, get_workspace_context

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_GITHUB_API_URL = "https://api.github.com"
_GITHUB_SSH_RE = re.compile(r"git@github\.com:([^/]+)/([^\.]+?)(?:\.git)?$")
_GITHUB_HTTPS_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")

WRITE_SPEC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {"type": "string", "description": "The workspace identifier."},
        "feature_id": {"type": "string", "description": "The feature identifier."},
        "content": {"type": "string", "description": "Full Markdown content to write to product-spec.md."},
        "commit_message": {"type": "string", "description": "Git commit message (optional)."},
    },
    "required": ["workspace_id", "feature_id", "content"],
    "additionalProperties": False,
}

WRITE_TD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {"type": "string", "description": "The workspace identifier."},
        "feature_id": {"type": "string", "description": "The feature identifier."},
        "content": {"type": "string", "description": "Full Markdown content to write to technical-design.md."},
        "commit_message": {"type": "string", "description": "Git commit message (optional)."},
    },
    "required": ["workspace_id", "feature_id", "content"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# GitHub helpers
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


def _github_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"token {token}", "Accept": "application/vnd.github.v3+json"}


def _get_file_sha(owner: str, repo: str, path: str, token: str) -> Optional[str]:
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
    resp = requests.get(url, headers=_github_headers(token), timeout=_DEFAULT_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json().get("sha")


def _put_file(owner: str, repo: str, path: str, content: str, commit_message: str, token: str) -> Dict[str, Any]:
    sha = _get_file_sha(owner, repo, path, token)
    payload: Dict[str, Any] = {
        "message": commit_message,
        "content": base64.b64encode(content.encode()).decode("ascii"),
    }
    if sha:
        payload["sha"] = sha
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
    resp = requests.put(url, headers={**_github_headers(token), "Content-Type": "application/json"},
                        json=payload, timeout=_DEFAULT_TIMEOUT)
    resp.raise_for_status()
    return resp.json()


def _write_artifact(
    workspace_id: str,
    feature_id: str,
    filename: str,
    content: str,
    commit_message: str,
) -> Dict[str, Any]:
    _validate_id(feature_id, "feature_id")
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    workspace_context = get_workspace_context(workspace_id)
    owner, repo = _resolve_management_repo(workspace_context)
    path = f"docs/features/{feature_id}/{filename}"
    result = _put_file(owner, repo, path, content, commit_message, github_token)
    return {"ok": True, "path": path, "commit": result.get("commit", {}).get("sha", "")}


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_write_product_spec(
    workspace_id: str,
    feature_id: str,
    content: str,
    commit_message: str = "docs: update product spec",
    **_: Any,
) -> Dict[str, Any]:
    try:
        return _write_artifact(workspace_id, feature_id, "product-spec.md", content, commit_message)
    except Exception as exc:
        logger.warning("workflow_write_product_spec failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def handle_write_technical_design(
    workspace_id: str,
    feature_id: str,
    content: str,
    commit_message: str = "docs: update technical design",
    **_: Any,
) -> Dict[str, Any]:
    try:
        return _write_artifact(workspace_id, feature_id, "technical-design.md", content, commit_message)
    except Exception as exc:
        logger.warning("workflow_write_technical_design failed: %s", exc)
        return {"ok": False, "error": str(exc)}
