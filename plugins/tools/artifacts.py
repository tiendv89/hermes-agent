"""write_product_spec / write_technical_design tools.

Full-rewrite handlers: write document content to storage-service.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


from ..validation import _validate_id
from ..document_repo import (
    branch_exists,
    ensure_feature_branch,
)
from ..storage_service_client import StorageServiceError, write_document_content

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
        "Write the feature's product-spec.md — stores the full Markdown to "
        "storage-service. Use when authoring or revising "
        "the product specification. The content must follow the product-spec "
        "template (see the content field); pass the complete document, not a diff. "
        "REQUIRED FIRST: gather repository context with query_rag and "
        "query_gitnexus (start with tool='list_repos') before calling this — "
        "ground the spec in real repos/tables/symbols, not assumptions. If both "
        "return nothing, note the unresolved questions in the document rather "
        "than inventing names. In a general-chat session (no current feature), "
        "pass the SAME feature_id to query_rag/query_gitnexus that you pass here "
        "— otherwise those calls credit no feature and this tool keeps reporting "
        "needs_context."
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
        "Write the feature's technical-design.md — stores the full Markdown to "
        "storage-service. Use when authoring or "
        "revising the technical design. The content must follow the "
        "technical-design template (see the content field); pass the complete "
        "document, not a diff. REQUIRED FIRST: call read_document(document='product_spec') "
        "to load the approved spec from the feature branch and ground the design in its "
        "actual scope (it works even when the spec is unmerged/unindexed) — never infer "
        "scope from RAG or the request text. Then gather repository context with "
        "query_rag and query_gitnexus (start with tool='list_repos', then "
        "tool='query'/'context'/'impact' for the symbols the design touches) "
        "before calling this — ground the design in real repos/files/symbols, "
        "not assumptions. If both return nothing, note the unresolved repo/symbol "
        "questions in the document rather than inventing names. In a general-chat "
        "session (no current feature), pass the SAME feature_id to "
        "query_rag/query_gitnexus that you pass here — otherwise those calls "
        "credit no feature and this tool keeps reporting needs_context."
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
# GitHub helpers — used by other modules (read.py, edit.py, approve.py, etc.)
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
# Branch resolution helper — used by read.py, edit.py, and other modules
# ---------------------------------------------------------------------------


def _resolve_document_branch(
    gh_owner: str,
    gh_repo: str,
    feature_id: str,
    init_pr_url: Optional[str],
    base_branch: str,
    github_token: str,
) -> Tuple[str, Optional[str]]:
    """Return (target_branch, known_pr_url) for a document write.

    Decision logic (backward-compatible):
    - init_pr_url non-null + init branch exists  → commit to feature/<slug>-init;
                                                    return (init branch, init_pr_url)
    - init_pr_url non-null + branch gone (merged) → commit to feature/<slug>;
                                                    return (feature branch, None)
    - init_pr_url null (pre-existing feature)     → commit to feature/<slug> directly;
                                                    return (feature branch, None)

    All git branches use the feature *slug* (feature_id here), never the UUID.
    """
    slug = feature_id
    init_branch = f"feature/{slug}-init"
    feature_branch = f"feature/{slug}"

    if init_pr_url and branch_exists(gh_owner, gh_repo, init_branch, github_token):
        return init_branch, init_pr_url

    ensure_feature_branch(gh_owner, gh_repo, slug, base_branch, github_token)
    return feature_branch, None


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
        # Model wrapped the markdown in {"content": "...", ...} — extract the string.
        if isinstance(content.get("content"), str):
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


def _write_artifact(
    workspace_id: str,
    feature_id: str,
    filename: str,
    content: str,
    commit_message: str,
    stage: str,
) -> Dict[str, Any]:
    """Write document content to storage-service."""
    _validate_id(feature_id, "feature_id")

    # Hard gate: a product spec / technical design must be grounded in the
    # indexed codebase. Block the write until the agent has gathered context via
    # query_rag or query_gitnexus for this feature in the session. Marking is set
    # by those tool handlers (see plugins/context.py); once set it persists, so
    # later revisions of the same doc are not re-blocked.
    from ..context import was_context_gathered

    if not was_context_gathered(feature_id):
        return {
            "ok": False,
            "needs_context": True,
            "error": (
                "Context not gathered. Before writing the "
                f"{stage.replace('_', ' ')}, you must ground it in the codebase: "
                "call query_rag for the feature's domain/entities, and "
                "query_gitnexus (tool='list_repos', then tool='query') for the "
                "relevant repos/symbols. Run at least one of them for this "
                "feature, then call this tool again. If both return nothing, that "
                "still satisfies this gate — record the unresolved questions in "
                "the document instead of inventing repo/table/symbol names."
            ),
        }

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

    from ..context import get_org_id, get_user_id

    caller_user_id = get_user_id()
    caller_org_id = get_org_id()

    storage_path_map = {
        "product_spec": "product_spec.md",
        "technical_design": "tech_design.md",
    }
    storage_path = storage_path_map.get(stage, stage)
    try:
        result = write_document_content(
            workspace_id,
            feature_id,
            storage_path,
            content,
            user_id=caller_user_id,
            org_id=caller_org_id,
        )
    except StorageServiceError as exc:
        logger.warning("_write_artifact: storage-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("_write_artifact: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "path": f"storage-service://{workspace_id}/{feature_id}/{storage_path}",
        "commit": None,
        "commit_sha": None,
        "pr_url": None,
        "conflict": False,
        "version_id": result.get("version_id"),
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
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }
    try:
        return _write_artifact(
            wid, fid, "product-spec.md", content, commit_message, stage="product_spec"
        )
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
    except Exception as exc:
        logger.warning("write_technical_design failed: %s", exc)
        return {"ok": False, "error": str(exc)}
