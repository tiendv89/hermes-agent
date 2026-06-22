"""edit_document — targeted str_replace edit tool.

The Canvas/Artifacts targeted-edit pattern: the agent emits a list of
{old_string, new_string} replacements; the module reads the current content,
applies them server-side, and commits. Small agent output, minimal clobber
surface compared to a full-rewrite.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List

from ..db import _validate_id, get_workspace_context
from ..document_repo import StaleBaseError, read_document, write_document
from .artifacts import _resolve_management_repo

logger = logging.getLogger(__name__)

_DOCUMENT_FILES: Dict[str, str] = {
    "product_spec": "product-spec.md",
    "technical_design": "technical-design.md",
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
    """Apply targeted edits to a document and commit to the feature branch."""
    from ..context import get_feature_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
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

    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    try:
        workspace_context = get_workspace_context(wid)
        owner, repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        return {"ok": False, "error": f"Could not resolve management repo: {exc}"}

    base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")
    branch = f"feature/{fid}"
    path = f"docs/features/{fid}/{filename}"

    try:
        current = read_document(owner, repo, branch, path, github_token)
        new_content, warnings = _apply_edits(current["content"], edits)
        if not commit_message:
            commit_message = f"docs: edit {document.replace('_', '-')} (targeted)"
        result = write_document(
            owner, repo, fid, base_branch, path, new_content, current["sha"], commit_message, github_token
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
