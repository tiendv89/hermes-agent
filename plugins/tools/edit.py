"""edit_document — targeted str_replace edit tool.

The Canvas/Artifacts targeted-edit pattern: the agent emits a list of
{old_string, new_string} replacements; the module reads the current content,
applies them server-side, and commits. Small agent output, minimal clobber
surface compared to a full-rewrite.

Owner guard: for go-owned features, document content lives in storage-service
(not git). In that case, edit_document proxies the read + apply + write to
storage-service using STORAGE_SERVICE_TOKEN. ts-owned and absent-owner features
continue to commit to git unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from ..validation import _validate_id
from src.services.workflow_backend_client import get_feature_detail, get_workspace_context, run_async
from ..document_repo import (
    StaleBaseError,
    commit_to_branch,
    ensure_pr_for_head,
    read_document,
    write_document,
)
from ..storage_service_client import StorageServiceError, read_document_content, write_document_content
from .artifacts import _resolve_document_branch, _resolve_management_repo

logger = logging.getLogger(__name__)

_DOCUMENT_FILES: Dict[str, str] = {
    "product_spec": "product-spec.md",
    "technical_design": "technical-design.md",
}

# storage-service document paths for go-owned features — distinct from
# _DOCUMENT_FILES (git filenames, hyphenated) since storage-service documents
# are identified by path directly (see storage_service_client.py's contract),
# matching workflow-backend's provisioning convention (feature_create.go).
_STORAGE_DOC_PATHS: Dict[str, str] = {
    "product_spec": "product_spec.md",
    "technical_design": "tech_design.md",
}

EDIT_DOCUMENT_SCHEMA: Dict[str, Any] = {
    "description": (
        "Make targeted find-and-replace edits to a feature document "
        "(product_spec or technical_design) and commit them to the feature "
        "branch. Prefer this over a full rewrite when changing specific passages."
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
            "document": {
                "type": "string",
                "enum": ["product_spec", "technical_design"],
                "description": "Which document to edit: 'product_spec' or 'technical_design'.",
            },
            "edits": {
                "type": "array",
                "description": "Ordered list of targeted replacements to apply to the current document content.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {
                            "type": "string",
                            "description": "Exact text to find in the document (must match exactly).",
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement text.",
                        },
                    },
                    "required": ["old_string", "new_string"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "commit_message": {
                "type": "string",
                "description": "Git commit message (optional).",
            },
        },
        "required": ["document", "edits"],
        "additionalProperties": False,
    },
}


def _apply_edits(content: str, edits: List[Dict[str, str]]) -> tuple[str, List[str]]:
    """Apply each replacement in order. Returns (new_content, list_of_warnings)."""
    warnings: List[str] = []
    for edit in edits:
        old = edit.get("old_string", "")
        new = edit.get("new_string", "")
        if old not in content:
            warnings.append(f"old_string not found: {old[:60]!r}")
            continue
        content = content.replace(old, new, 1)
    return content, warnings


def handle_edit_document(
    document: str,
    edits: List[Dict[str, str]],
    workspace_id: str = "",
    feature_id: str = "",
    commit_message: str = "",
    **_: Any,
) -> Dict[str, Any]:
    """Apply targeted edits to a document and commit to the feature branch.

    For go-owned features, proxies the read + apply + write to storage-service.
    """
    from ..context import get_feature_id, get_org_id, get_user_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
    # Capture identity on this (calling) thread — run_async may bridge onto a
    # different thread, where thread-local context is unset.
    caller_user_id = get_user_id()
    caller_org_id = get_org_id()
    if not wid or not fid:
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }

    filename = _DOCUMENT_FILES.get(document)
    if filename is None:
        return {"ok": False, "error": f"Unknown document type: {document!r}. Must be one of {list(_DOCUMENT_FILES)}."}

    try:
        _validate_id(fid, "feature_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # Resolve owner + slug to decide which backend to use.
    owner_value: str = "ts"
    slug = fid
    init_pr_url = None
    try:
        detail = run_async(get_feature_detail(wid, fid, user_id=caller_user_id, org_id=caller_org_id))
        slug = detail.get("feature_name") or fid
        init_pr_url = detail.get("init_pr_url")
        owner_value = detail.get("owner") or "ts"
    except Exception as exc:
        logger.debug("edit_document: could not fetch feature_detail: %s", exc)

    # Owner guard: go-owned features proxy read + apply + write to storage-service.
    if owner_value == "go":
        storage_path = _STORAGE_DOC_PATHS.get(document, filename)
        try:
            read_result = read_document_content(
                wid, fid, storage_path,
                user_id=caller_user_id, org_id=caller_org_id,
            )
            current_content = read_result.get("content", "")
            new_content, warnings = _apply_edits(current_content, edits)
            write_result = write_document_content(
                wid, fid, storage_path, new_content,
                user_id=caller_user_id, org_id=caller_org_id,
            )
        except StorageServiceError as exc:
            logger.warning("edit_document (go): storage-service error: %s", exc)
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.warning("edit_document (go): unexpected error: %s", exc)
            return {"ok": False, "error": str(exc)}
        out: Dict[str, Any] = {
            "ok": True,
            "pr_url": None,
            "commit_sha": None,
            "conflict": False,
            "version_id": write_result.get("version_id"),
        }
        if warnings:
            out["warnings"] = warnings
        return out

    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    try:
        workspace_context = run_async(get_workspace_context(wid, user_id=caller_user_id, org_id=caller_org_id))
        owner, repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        return {"ok": False, "error": f"Could not resolve management repo: {exc}"}

    base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")

    target_branch, known_pr_url = _resolve_document_branch(
        owner, repo, fid, slug, init_pr_url, base_branch, github_token
    )
    path = f"docs/features/{slug}/{filename}"

    try:
        current = read_document(owner, repo, target_branch, path, github_token)
        new_content, warnings = _apply_edits(current["content"], edits)
        if not commit_message:
            commit_message = f"docs: edit {document.replace('_', '-')} (targeted)"
        if target_branch.endswith("-init"):
            # Init branch is the canonical design-phase branch: commit there and
            # keep a single init PR. Mirrors _write_artifact — do NOT gate this on
            # known_pr_url, or an init feature whose init_pr_url wasn't recorded
            # would have its edits land on feature/{slug} instead of -init.
            commit_sha = commit_to_branch(
                owner, repo, target_branch, path, new_content, current["sha"], commit_message, github_token
            )
            pr_url = known_pr_url
            if not pr_url:
                pr = ensure_pr_for_head(
                    owner,
                    repo,
                    target_branch,
                    base_branch,
                    github_token,
                    title=f"docs({slug}): product spec + technical design",
                    body=f"Design-phase authoring PR for feature `{slug}` (hermes-agent).",
                )
                pr_url = pr.get("url", "")
            result = {"commit_sha": commit_sha, "pr": {"url": pr_url or ""}}
        else:
            result = write_document(
                owner, repo, slug, base_branch, path, new_content, current["sha"], commit_message, github_token
            )
    except StaleBaseError as exc:
        return {"ok": False, "conflict": True, "error": str(exc)}
    except Exception as exc:
        logger.warning("edit_document failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    out: Dict[str, Any] = {
        "ok": True,
        "pr_url": result["pr"].get("url", ""),
        "commit_sha": result["commit_sha"],
        "conflict": False,
    }
    if warnings:
        out["warnings"] = warnings
    return out
