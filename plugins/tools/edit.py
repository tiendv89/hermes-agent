"""edit_document — targeted str_replace edit tool.

The Canvas/Artifacts targeted-edit pattern: the agent emits a list of
{old_string, new_string} replacements; the module reads the current content
from storage-service, applies them server-side, and writes it back. Small
agent output, minimal clobber surface compared to a full-rewrite.
"""

from __future__ import annotations

import logging
from typing import Any

from plugins.clients.storage_service_client import (
    StorageServiceError,
    read_document_content,
    write_document_content,
)

from ..validation import _validate_id

logger = logging.getLogger(__name__)

_STORAGE_DOC_PATHS: dict[str, str] = {
    "product_spec": "product_spec.md",
    "technical_design": "tech_design.md",
}

EDIT_DOCUMENT_SCHEMA: dict[str, Any] = {
    "description": (
        "Make targeted find-and-replace edits to a feature document "
        "(product_spec or technical_design) and write them to storage-service. "
        "Prefer this over a full rewrite when changing specific passages."
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
        },
        "required": ["document", "edits"],
        "additionalProperties": False,
    },
}


def _apply_edits(content: str, edits: list[dict[str, str]]) -> tuple[str, list[str]]:
    """Apply each replacement in order. Returns (new_content, list_of_warnings)."""
    warnings: list[str] = []
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
    edits: list[dict[str, str]],
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Apply targeted edits to a document via read + apply + write to storage-service."""
    from ..context import get_feature_id, get_org_id, get_user_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
    caller_user_id = get_user_id()
    caller_org_id = get_org_id()
    if not wid or not fid:
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }

    storage_path = _STORAGE_DOC_PATHS.get(document)
    if storage_path is None:
        return {"ok": False, "error": f"Unknown document type: {document!r}. Must be one of {list(_STORAGE_DOC_PATHS)}."}

    try:
        _validate_id(fid, "feature_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

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
        logger.warning("edit_document: storage-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("edit_document: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}

    out: dict[str, Any] = {
        "ok": True,
        "pr_url": None,
        "commit_sha": None,
        "conflict": False,
        "version_id": write_result.get("version_id"),
    }
    if warnings:
        out["warnings"] = warnings
    return out
