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


from ..db import _validate_id, get_feature_detail, get_workspace_context
from ..document_repo import (
    StaleBaseError,
    branch_exists,
    commit_to_branch,
    ensure_feature_branch,
    read_document,
    write_document,
)

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_GITHUB_API_URL = "https://api.github.com"
_GITHUB_SSH_RE = re.compile(r"git@github\.com:([^/]+)/([^\.]+?)(?:\.git)?$")
_GITHUB_HTTPS_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")

# Canonical feature-doc templates the generated artifacts must follow. Bundled
# under plugins/skills/templates/feature/ (a copy of agent-workflow/templates).
_TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "skills" / "templates" / "feature"
)


def _load_template(filename: str) -> str:
    """Return a bundled feature-doc template, or '' if it can't be read."""
    try:
        return (_TEMPLATES_DIR / filename).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        logger.warning(
            "artifacts: template %s not found under %s", filename, _TEMPLATES_DIR
        )
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
    "description": (
        "Write the feature's product-spec.md — commits the full Markdown to the "
        "feature branch (opening/updating its PR). Use when authoring or revising "
        "the product specification. The content must follow the product-spec "
        "template (see the content field); pass the complete document, not a diff."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier. Omit to use the current workspace from context.",
            },
            "feature_id": {
                "type": "string",
                "description": "Feature identifier. Omit to use the current feature from context.",
            },
            "content": {
                "type": "string",
                "description": _content_description(
                    "product-spec.md", _PRODUCT_SPEC_TEMPLATE
                ),
            },
            "commit_message": {
                "type": "string",
                "description": "Git commit message (optional).",
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    },
}

WRITE_TD_SCHEMA: Dict[str, Any] = {
    "description": (
        "Write the feature's technical-design.md — commits the full Markdown to "
        "the feature branch (opening/updating its PR). Use when authoring or "
        "revising the technical design. The content must follow the "
        "technical-design template (see the content field); pass the complete "
        "document, not a diff."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier. Omit to use the current workspace from context.",
            },
            "feature_id": {
                "type": "string",
                "description": "Feature identifier. Omit to use the current feature from context.",
            },
            "content": {
                "type": "string",
                "description": _content_description(
                    "technical-design.md", _TECHNICAL_DESIGN_TEMPLATE
                ),
            },
            "commit_message": {
                "type": "string",
                "description": "Git commit message (optional).",
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# GitHub helpers — kept for backward compat and use in document_repo resolution
# ---------------------------------------------------------------------------


def _parse_github_owner_repo(github_url: str) -> Tuple[str, str]:
    m = _GITHUB_SSH_RE.match(github_url.strip()) or _GITHUB_HTTPS_RE.match(
        github_url.strip()
    )
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
        if (
            isinstance(repo, dict)
            and "management" in repo.get("id", "")
            and repo.get("github")
        ):
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
    model occasionally passes a structured JSON object instead of markdown. That
    dict would otherwise reach ``str.encode`` in document_repo and raise
    ``'dict' object has no attribute 'encode'``.

    An empty dict/list (``{}``, ``[]``) is a model mistake — the model called
    the tool without generating actual content. We raise ValueError so the
    caller returns an error rather than committing useless bytes to GitHub.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if not content:
            raise ValueError(
                "content is an empty object — the model did not generate document "
                "content before calling the tool. Regenerate the full markdown "
                "document and pass it as the content string."
            )
        # Model wrapped the markdown in {"content": "..."} — unwrap it.
        if set(content.keys()) <= {"content"} and isinstance(content.get("content"), str):
            return content["content"]
        return json.dumps(content, indent=2, ensure_ascii=False)
    if isinstance(content, list):
        if not content:
            raise ValueError(
                "content is an empty list — the model did not generate document "
                "content before calling the tool. Regenerate the full markdown "
                "document and pass it as the content string."
            )
        return json.dumps(content, indent=2, ensure_ascii=False)
    return str(content)


def _owner_guard_ts_only(owner: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return a skip response for go features; return None to allow ts/absent features to proceed.

    Applied before operations that are only valid for TypeScript/git (ts) features,
    such as writing task YAML files or creating task-state git branches.
    Document writes (product-spec, technical-design) are NOT blocked — use branch
    decision logic there instead.
    """
    if owner == "go":
        return {
            "ok": False,
            "skipped": True,
            "reason": (
                "This operation is only available for TypeScript/git (ts) features. "
                "The current feature uses the Postgres/Go (go) orchestrator — "
                "task YAML files and task-state branches are managed by the database, "
                "not by git."
            ),
        }
    return None


def _resolve_document_branch(
    gh_owner: str,
    gh_repo: str,
    feature_id: str,
    feature_name: Optional[str],
    init_pr_url: Optional[str],
    base_branch: str,
    github_token: str,
) -> Tuple[str, Optional[str]]:
    """Return (target_branch, known_pr_url) for the document write.

    Decision logic (backward-compatible):
    - init_pr_url non-null + init branch exists  → commit to feature/<slug>-init;
                                                    return (init branch, init_pr_url)
    - init_pr_url non-null + branch gone (merged) → commit to feature/<id>;
                                                    return (feature branch, None)
    - init_pr_url null (pre-existing feature)     → commit to feature/<id> directly;
                                                    return (feature branch, None)

    The init branch uses the feature *slug* (feature_name) because that is what
    workflow-backend creates: ``feature/{slug}-init``. The feature UUID is used
    for the ongoing feature branch (``feature/{uuid}``).
    """
    # Init branch: workflow-backend names it feature/{slug}-init, not feature/{uuid}-init.
    init_slug = feature_name or feature_id
    init_branch = f"feature/{init_slug}-init"
    feature_branch = f"feature/{feature_id}"

    if init_pr_url:
        if branch_exists(gh_owner, gh_repo, init_branch, github_token):
            return init_branch, init_pr_url
        # Branch gone — init PR was merged; fall through to feature branch.

    # Ensure feature/<id> exists before we try to read/write it.
    ensure_feature_branch(gh_owner, gh_repo, feature_id, base_branch, github_token)
    return feature_branch, None


def _write_artifact(
    workspace_id: str,
    feature_id: str,
    filename: str,
    content: str,
    commit_message: str,
    stage: str,
) -> Dict[str, Any]:
    """Full-rewrite path: determine target branch, write document, call request_approval."""
    _validate_id(feature_id, "feature_id")
    try:
        content = _coerce_content(content)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not content.strip():
        return {
            "ok": False,
            "error": (
                "content is empty — the document was not written to GitHub. "
                "Generate the full markdown document and pass it as the content string."
            ),
        }
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    workspace_context = get_workspace_context(workspace_id)
    gh_owner, gh_repo = _resolve_management_repo(workspace_context)
    base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")

    # Look up init_pr_url and feature_name from the DB if WORKFLOW_DATABASE_URL is available.
    init_pr_url: Optional[str] = None
    feature_name: Optional[str] = None
    try:
        feature_detail = get_feature_detail(workspace_id, feature_id)
        init_pr_url = feature_detail.get("init_pr_url")
        feature_name = feature_detail.get("feature_name")
    except Exception as exc:
        logger.debug("_write_artifact: could not fetch feature_detail: %s", exc)

    target_branch, known_pr_url = _resolve_document_branch(
        gh_owner, gh_repo, feature_id, feature_name, init_pr_url, base_branch, github_token
    )

    # Use the feature slug for the docs path on the init branch (workflow-backend
    # scaffolds files at docs/features/{slug}/), and the UUID on the feature branch.
    doc_dir = (feature_name or feature_id) if target_branch.endswith("-init") else feature_id
    path = f"docs/features/{doc_dir}/{filename}"

    # Read-before-write: fetch the current SHA so GitHub accepts our PUT.
    current = read_document(gh_owner, gh_repo, target_branch, path, github_token)

    if target_branch.endswith("-init") and known_pr_url:
        # Init PR branch path: commit directly; PR URL is already known.
        commit_sha = commit_to_branch(
            gh_owner,
            gh_repo,
            target_branch,
            path,
            content,
            current["sha"],
            commit_message,
            github_token,
        )
        pr_url = known_pr_url
    else:
        # Feature branch path: write via the standard pipeline (ensures PR exists).
        result = write_document(
            gh_owner,
            gh_repo,
            feature_id,
            base_branch,
            path,
            content,
            current["sha"],
            commit_message,
            github_token,
        )
        commit_sha = result["commit_sha"]
        pr_url = result["pr"].get("url", "")

    # Call request_approval so the approve button appears in the feature detail view.
    approval_request: Optional[Dict[str, Any]] = None
    try:
        from .approval import handle as _request_approval

        approval_result = _request_approval(stage=stage, feature_id=feature_id)
        if approval_result.get("ok"):
            approval_request = approval_result.get("approval_request")
    except Exception as exc:
        logger.warning("_write_artifact: request_approval failed: %s", exc)

    out: Dict[str, Any] = {
        "ok": True,
        "path": path,
        "commit": commit_sha,
        "commit_sha": commit_sha,
        "pr_url": pr_url,
        "conflict": False,
    }
    if approval_request:
        out["approval_request"] = approval_request
    return out


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
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }
    try:
        return _write_artifact(
            wid, fid, "product-spec.md", content, commit_message, stage="product_spec"
        )
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
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }
    try:
        return _write_artifact(
            wid,
            fid,
            "technical-design.md",
            content,
            commit_message,
            stage="technical_design",
        )
    except StaleBaseError as exc:
        return {"ok": False, "conflict": True, "error": str(exc)}
    except Exception as exc:
        logger.warning("write_technical_design failed: %s", exc)
        return {"ok": False, "error": str(exc)}
