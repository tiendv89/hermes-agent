"""write_product_spec / write_technical_design tools.

Full-rewrite handlers: read the current document SHA, replace the entire
content, and commit to the feature branch via the document_repo pipeline.
This replaces the old direct-to-main Contents PUT and fixes the
"no direct push to main" rule violation.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


from ..db import _validate_id, get_workspace_context
from ..document_repo import StaleBaseError, read_document, write_document

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_GITHUB_API_URL = "https://api.github.com"
_GITHUB_SSH_RE = re.compile(r"git@github\.com:([^/]+)/([^\.]+?)(?:\.git)?$")
_GITHUB_HTTPS_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")

# Canonical feature-doc templates the generated artifacts must follow. Bundled
# under plugins/skills/templates/feature/ (a copy of agent-workflow/templates).
_TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "skills" / "templates" / "feature"


def _load_template(filename: str) -> str:
    """Return a bundled feature-doc template, or '' if it can't be read."""
    try:
        return (_TEMPLATES_DIR / filename).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        logger.warning("artifacts: template %s not found under %s", filename, _TEMPLATES_DIR)
        return ""


def _content_description(filename: str, template: str) -> str:
    """Build the ``content`` field description, embedding the template so the
    agent generates a document that matches its structure exactly."""
    desc = (
        f"Full Markdown content for {filename} — the complete document, not a diff. "
        f"Follow the template below exactly: keep every section heading and their "
        f"order, fill each section with real content, and replace the <...> "
        f"placeholders. You may add subsections within a section, but do not drop "
        f"or rename the template's headings."
    )
    if template:
        desc += f"\n\n--- TEMPLATE ({filename}) ---\n{template}\n--- END TEMPLATE ---"
    return desc


_PRODUCT_SPEC_TEMPLATE = _load_template("product-spec.md")
_TECHNICAL_DESIGN_TEMPLATE = _load_template("technical-design.md")

WRITE_SPEC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Write the feature's product-spec.md — commits the full Markdown to the "
        "feature branch (opening/updating its PR). Use when authoring or revising "
        "the product specification. The content must follow the product-spec "
        "template (see the content field); pass the complete document, not a diff."
    ),
    "properties": {
        "workspace_id": {"type": "string", "description": "Workspace identifier. Omit to use the current workspace from context."},
        "feature_id": {"type": "string", "description": "Feature identifier. Omit to use the current feature from context."},
        "content": {"type": "string", "description": _content_description("product-spec.md", _PRODUCT_SPEC_TEMPLATE)},
        "commit_message": {"type": "string", "description": "Git commit message (optional)."},
    },
    "required": ["content"],
    "additionalProperties": False,
}

WRITE_TD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Write the feature's technical-design.md — commits the full Markdown to "
        "the feature branch (opening/updating its PR). Use when authoring or "
        "revising the technical design. The content must follow the "
        "technical-design template (see the content field); pass the complete "
        "document, not a diff."
    ),
    "properties": {
        "workspace_id": {"type": "string", "description": "Workspace identifier. Omit to use the current workspace from context."},
        "feature_id": {"type": "string", "description": "Feature identifier. Omit to use the current feature from context."},
        "content": {"type": "string", "description": _content_description("technical-design.md", _TECHNICAL_DESIGN_TEMPLATE)},
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


def _management_repo_from_env() -> Optional[Tuple[str, str]]:
    """Env fallback for the management repo when the workspace context has no
    github source configured. Accepts a git URL or a plain ``owner/repo``.

    Set ``MANAGEMENT_REPO_GITHUB`` to unblock document writes when the
    workflow-backend DB has no ``workspace_github_sources.repo_url`` row yet.
    """
    env_repo = os.environ.get("MANAGEMENT_REPO_GITHUB", "").strip()
    if not env_repo:
        return None
    try:
        return _parse_github_owner_repo(env_repo)
    except ValueError:
        owner, _, repo = env_repo.partition("/")
        repo = repo[:-4] if repo.endswith(".git") else repo
        if owner and repo and "/" not in repo:
            return owner, repo
    return None


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

    env_fallback = _management_repo_from_env()
    if env_fallback is not None:
        return env_fallback

    raise ValueError(
        f"Could not resolve management repo: the workspace has no github source "
        f"configured (management_repo={management_repo_id!r}, repos={repos!r}). "
        f"Configure the workspace's repo in the workflow-backend, or set "
        f"MANAGEMENT_REPO_GITHUB (a git URL or 'owner/repo') to override."
    )


# ---------------------------------------------------------------------------
# Internal write pipeline
# ---------------------------------------------------------------------------

def _coerce_content(content: Any) -> str:
    """Normalize tool ``content`` to a string before it is UTF-8 encoded.

    The tool schema declares ``content`` as a string, but over the MCP path the
    model frequently passes a structured JSON object instead. That dict would
    otherwise reach ``str.encode`` in document_repo and raise
    ``'dict' object has no attribute 'encode'``.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, (dict, list)):
        return json.dumps(content, indent=2, ensure_ascii=False)
    return str(content)


def _write_artifact(
    workspace_id: str,
    feature_id: str,
    filename: str,
    content: str,
    commit_message: str,
) -> Dict[str, Any]:
    """Full-rewrite path: read current SHA then write to feature/<feature_id>."""
    _validate_id(feature_id, "feature_id")
    content = _coerce_content(content)
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
        logger.warning("write_product_spec failed: %s", exc)
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
        logger.warning("write_technical_design failed: %s", exc)
        return {"ok": False, "error": str(exc)}
