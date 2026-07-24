"""read_workspace_file — read a workspace-root document from storage-service.

Digital Factory's Files browser lets a user upload a file directly at the
workspace root (e.g. a shared asset), not owned by any feature. Those
documents aren't reachable through read_file (which always requires a
feature_id) or through GitNexus/RAG (which only index git-tracked feature
docs and explicitly-embedded content respectively) — this tool is the only
path to their content. See storage_service_client.py's module docstring for
the underlying no-feature content endpoint.
"""

from __future__ import annotations

import logging
from typing import Any

from plugins.clients.storage_service_client import (
    StorageServiceError,
    read_document_content,
)

from ..validation import _validate_id

logger = logging.getLogger(__name__)

SCHEMA: dict[str, Any] = {
    "description": (
        "Read a workspace-root file's content — a file uploaded directly at the workspace level "
        "in the Files browser, not owned by any feature (e.g. a shared asset or doc dropped outside "
        "a feature's folder). Not reachable via read_file (which requires a feature_id) or via "
        "query_gitnexus/query_rag (which only cover git-tracked feature docs and indexed content). "
        "Pass the file's path relative to the workspace root, exactly as shown in the Files browser "
        "or a #_workspace/{path} chat mention (e.g. 'tests/api.go')."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier. Omit to use the current workspace from context.",
            },
            "path": {
                "type": "string",
                "description": "The file's path relative to the workspace root (e.g. 'tests/api.go').",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}


def handle(path: str = "", workspace_id: str = "", **_: Any) -> dict[str, Any]:
    from ..context import get_org_id, get_user_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    caller_user_id = get_user_id()
    caller_org_id = get_org_id()

    if not wid:
        return {"ok": False, "error": "workspace_id is required but was not provided and no context is set."}
    if not path or not path.strip():
        return {"ok": False, "error": "path is required."}

    try:
        _validate_id(wid, "workspace_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        result = read_document_content(wid, "", path, user_id=caller_user_id, org_id=caller_org_id)
    except StorageServiceError as exc:
        logger.warning("read_workspace_file: storage-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("read_workspace_file: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}

    content = result.get("content", "")
    return {
        "ok": True,
        "exists": bool(content),
        "path": path,
        "content": content,
        "sha": result.get("version_id"),
    }
