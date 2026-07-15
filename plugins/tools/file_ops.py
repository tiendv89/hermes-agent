"""write_file / edit_file — generic arbitrary-filename write/edit tools.

These tools mirror how read_file already accepts an arbitrary filename beyond
the two canonical documents (product_spec / technical_design). They write through
storage_service_client.py, the same client used by write_product_spec /
write_technical_design.

feature_id is optional. When one is given (explicitly or via context), the file
is written to that feature's document folder. With no feature_id, the file is
written directly under the workspace root instead.

See technical-design.md §Chosen Design for the full rationale (Option B).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from plugins.clients.storage_service_client import (
    StorageServiceError,
    read_document_content,
    write_document_content,
)

logger = logging.getLogger(__name__)


def _validate_path(path: str) -> str | None:
    """Return an error message if path is unsafe, or None if it is valid.

    A safe path must be:
    - non-empty
    - relative (no leading '/')
    - free of '..' segments (no path traversal)
    """
    if not path:
        return "path must not be empty"
    if path.startswith("/"):
        return f"path must be relative (no leading '/'): {path!r}"
    parts = path.replace("\\", "/").split("/")
    if ".." in parts:
        return f"path must not contain '..' segments: {path!r}"
    return None


WRITE_FILE_SCHEMA: Dict[str, Any] = {
    "description": (
        "Create or overwrite an arbitrary named file in storage-service — within a "
        "go-owned feature's document folder if a feature_id is given (explicitly or "
        "via context), or directly under the workspace root otherwise. Use for any "
        "filename that is not one of the canonical documents (product-spec.md, "
        "technical-design.md) — e.g. 'notes.md', 'handoffs/handoff.md', "
        "'research-notes.md'. No feature_id is required."
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
                "description": (
                    "Feature identifier. Omit to use the current feature from context. "
                    "Not required — if there is no current feature in context either, "
                    "the file is written directly under the workspace root instead of "
                    "a feature's document folder."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Relative path of the file within the feature's document folder, or "
                    "the workspace root if no feature_id applies (e.g. 'notes.md', "
                    "'handoffs/handoff.md'). Must not start with '/' and must not contain "
                    "'..' segments. A brand-new path is created automatically — no need to "
                    "ask the user for one first."
                ),
            },
            "content": {
                "type": "string",
                "description": "Full text content to write. Overwrites any existing content at this path.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
}

EDIT_FILE_SCHEMA: Dict[str, Any] = {
    "description": (
        "Make targeted find-and-replace edits to an arbitrary named file in "
        "storage-service — within a go-owned feature's document folder if a "
        "feature_id is given (explicitly or via context), or directly under the "
        "workspace root otherwise. Reads the current content first (read-before-write), "
        "applies the ordered list of edits, then writes back. No feature_id is required."
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
                "description": (
                    "Feature identifier. Omit to use the current feature from context. "
                    "Not required — if there is no current feature in context either, "
                    "the file is written directly under the workspace root instead of "
                    "a feature's document folder."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Relative path of the file within the feature's document folder, or "
                    "the workspace root if no feature_id applies (e.g. 'notes.md', "
                    "'handoffs/handoff.md'). Must not start with '/' and must not contain "
                    "'..' segments. A brand-new path is created automatically — no need to "
                    "ask the user for one first."
                ),
            },
            "edits": {
                "type": "array",
                "description": "Ordered list of targeted replacements to apply to the current file content.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {
                            "type": "string",
                            "description": "Exact text to find in the file (must match exactly).",
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
        "required": ["path", "edits"],
        "additionalProperties": False,
    },
}


def handle_write_file(
    path: str,
    content: str,
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> Dict[str, Any]:
    """Create or overwrite an arbitrary file in storage-service.

    Writes to the given (or context) feature's document folder — go-owned
    features only — or, when no feature_id is available, directly under the
    workspace root. Returns {ok, path, version_id} on success.
    """
    from ..context import get_feature_id, get_org_id, get_user_id, get_workspace_id
    from src.services.workflow_backend_client import get_feature_detail, run_async

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
    caller_user_id = get_user_id()
    caller_org_id = get_org_id()

    if not wid:
        return {
            "ok": False,
            "error": "workspace_id is required but was not provided and no context is set.",
        }

    path_error = _validate_path(path)
    if path_error:
        return {"ok": False, "error": path_error}

    slug = ""
    if fid:
        try:
            detail = run_async(
                get_feature_detail(wid, fid, user_id=caller_user_id, org_id=caller_org_id)
            )
            owner = detail.get("owner") or "ts"
            slug = detail.get("feature_name") or ""
        except Exception as exc:
            logger.warning("write_file: could not fetch feature_detail: %s", exc)
            return {"ok": False, "error": f"Could not determine feature owner: {exc}"}

        if owner != "go":
            return {
                "ok": False,
                "error": "unsupported_owner",
                "message": "write_file is only supported for go-owned features",
            }

        if not slug:
            # storage-service only assigns the human-readable
            # docs/features/{slug}/ folder on this path's FIRST write (see
            # write_document_content docstring) — a later write to the same
            # path is edit-only and can't correct it. Writing now with an
            # empty slug would permanently strand this path under a raw-UUID
            # folder, so refuse instead of silently doing that.
            return {
                "ok": False,
                "error": (
                    f"Could not resolve a feature_name/slug for feature_id={fid!r} — "
                    "refusing to create a new document without one, since the first "
                    "write permanently fixes the folder name (raw feature_id vs. "
                    "slug) for this path. Retry, or confirm the feature via "
                    "workflow_lookup_feature first."
                ),
            }

    try:
        write_result = write_document_content(
            wid,
            fid,
            path,
            content,
            user_id=caller_user_id,
            org_id=caller_org_id,
            feature_slug=slug,
        )
    except StorageServiceError as exc:
        logger.warning("write_file: storage-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("write_file: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}

    return {
        "ok": True,
        "path": path,
        "version_id": write_result.get("version_id"),
    }


def handle_edit_file(
    path: str,
    edits: List[Dict[str, str]],
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> Dict[str, Any]:
    """Apply targeted find-and-replace edits to an arbitrary file.

    Reads current content first (read-before-write), applies edits via
    _apply_edits, then writes back. Targets the given (or context) feature's
    document folder — go-owned features only — or, when no feature_id is
    available, directly under the workspace root.
    Returns {ok, path, version_id} on success.
    """
    from ..context import get_feature_id, get_org_id, get_user_id, get_workspace_id
    from src.services.workflow_backend_client import get_feature_detail, run_async

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
    caller_user_id = get_user_id()
    caller_org_id = get_org_id()

    if not wid:
        return {
            "ok": False,
            "error": "workspace_id is required but was not provided and no context is set.",
        }

    path_error = _validate_path(path)
    if path_error:
        return {"ok": False, "error": path_error}

    slug = ""
    if fid:
        try:
            detail = run_async(
                get_feature_detail(wid, fid, user_id=caller_user_id, org_id=caller_org_id)
            )
            owner = detail.get("owner") or "ts"
            slug = detail.get("feature_name") or ""
        except Exception as exc:
            logger.warning("edit_file: could not fetch feature_detail: %s", exc)
            return {"ok": False, "error": f"Could not determine feature owner: {exc}"}

        if owner != "go":
            return {
                "ok": False,
                "error": "unsupported_owner",
                "message": "edit_file is only supported for go-owned features",
            }

    try:
        from .edit import (
            _apply_edits,
        )  # lazy import avoids edit.py's heavy dependency chain at module load

        read_result = read_document_content(
            wid,
            fid,
            path,
            user_id=caller_user_id,
            org_id=caller_org_id,
        )
        current_content = read_result.get("content", "")
        new_content, warnings = _apply_edits(current_content, edits)
    except StorageServiceError as exc:
        logger.warning("edit_file: storage-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("edit_file: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}

    try:
        write_result = write_document_content(
            wid,
            fid,
            path,
            new_content,
            user_id=caller_user_id,
            org_id=caller_org_id,
            feature_slug=slug,
        )
    except StorageServiceError as exc:
        logger.warning("edit_file: storage-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("edit_file: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}

    out: Dict[str, Any] = {
        "ok": True,
        "path": path,
        "version_id": write_result.get("version_id"),
    }
    if warnings:
        out["warnings"] = warnings
    return out
