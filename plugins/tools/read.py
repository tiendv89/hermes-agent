"""read_file — read a feature document from storage-service.

Feature documents (product-spec.md, technical-design.md) and status live in
storage-service and workflow-backend respectively — not in a git repository.
This tool proxies to storage-service's document-content endpoint (using
STORAGE_SERVICE_TOKEN) for product_spec/technical_design/tasks, and to
get_feature_detail for status.
"""

from __future__ import annotations

import logging
from typing import Any

import yaml

from plugins.clients.storage_service_client import (
    StorageServiceError,
    read_document_content,
)
from src.services.workflow_backend_client import get_feature_detail, run_async

from ..validation import _validate_id

logger = logging.getLogger(__name__)

# storage-service document paths for the canonical documents — anything else
# is treated as a literal path within the feature's document folder.
_STORAGE_DOC_PATHS: dict[str, str] = {
    "product_spec": "product_spec.md",
    "technical_design": "tech_design.md",
}

READ_FILE_SCHEMA: dict[str, Any] = {
    "description": (
        "Read a feature document straight from storage-service. Use this FIRST when writing or "
        "revising a technical design: call read_file(document='product_spec') to load the "
        "approved product spec and ground the design in its actual scope — do NOT infer the spec "
        "from RAG or the request text. This reads storage-service directly, so it works even "
        "when the document is unmerged/unapproved and not yet indexed by RAG. Pass "
        "'product_spec', 'technical_design', or 'status' for the three canonical documents, or "
        "any other filename (e.g. 'README.md', 'handoffs/handoff.md') to read an arbitrary file "
        "from the feature's document folder. Returns the document content and whether it exists."
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
                "description": (
                    "Which document to read. Use 'product_spec', 'technical_design', or 'status' "
                    "for the three canonical documents, or pass any other filename (e.g. "
                    "'README.md', 'handoffs/handoff.md') to read an arbitrary file from the "
                    "feature's document folder."
                ),
            },
        },
        "required": ["document"],
        "additionalProperties": False,
    },
}


def handle_read_file(
    document: str,
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Read a feature document from the resolved feature branch in git.

    For go-owned features, proxies to storage-service instead of git.
    """
    from ..context import (
        get_feature_id,
        get_org_id,
        get_user_id,
        get_workspace_id,
        mark_context_gathered,
    )

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

    if not document.strip():
        return {"ok": False, "error": "document is required."}

    try:
        _validate_id(fid, "feature_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    if document == "status":
        # Status lives entirely in workflow-backend's DB — synthesize
        # status.yaml-shaped content from the feature detail.
        try:
            detail = run_async(get_feature_detail(wid, fid, user_id=caller_user_id, org_id=caller_org_id))
        except Exception as exc:
            return {"ok": False, "error": f"Could not fetch feature detail: {exc}"}

        status_data = {
            "feature_status": detail.get("status") or "",
            "current_stage": detail.get("stage") or "",
            "next_action": detail.get("next_action") or "",
            "stages": dict(detail.get("stages") or {}),
        }
        content = yaml.dump(status_data, default_flow_style=False, allow_unicode=True)
        mark_context_gathered(fid)
        return {
            "ok": True,
            "exists": True,
            "document": document,
            "path": f"workflow-backend://{wid}/{fid}/status",
            "branch": None,
            "content": content,
            "sha": None,
        }

    storage_path = _STORAGE_DOC_PATHS.get(document, document)
    try:
        result = read_document_content(
            wid, fid, storage_path,
            user_id=caller_user_id, org_id=caller_org_id,
        )
    except StorageServiceError as exc:
        logger.warning("read_document: storage-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("read_document: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}

    content = result.get("content", "")
    exists = bool(content)
    if exists:
        mark_context_gathered(fid)
    return {
        "ok": True,
        "exists": exists,
        "document": document,
        "path": f"storage-service://{wid}/{fid}/{storage_path}",
        "branch": None,
        "content": content,
        "sha": result.get("version_id"),
    }
