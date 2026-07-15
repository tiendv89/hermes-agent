"""list_documents — walk a workspace's document folder via storage-service.

storage-service has no folder/tree entity of its own: "folders" are just a
path prefix shared by documents (see RenameFolder/DeleteFolder in
storage-service's handler.go — "Folders have no row of their own"). This tool
fetches the flat document list for the workspace (or one feature) and derives
the immediate children of a given path — the subfolder names and the files —
so the agent can walk the tree one directory at a time the way it would a
local filesystem, without having to reason about the full flat list itself.

Only sees documents that have a row in storage-service — go-owned feature
documents and workspace-root files (see storage_service_client.list_documents).
ts-owned feature documents live in git; read them directly with read_file,
there is no folder listing for that path yet.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from plugins.clients.storage_service_client import StorageServiceError
from plugins.clients.storage_service_client import list_documents as _list_documents
from ..validation import _validate_id

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "description": (
        "Walk a workspace's document folder like a local filesystem — go-owned/workspace-root "
        "documents only (storage-service backed; ts-owned feature docs live in git and aren't "
        "listed here, read them directly with read_file). Root is the workspace itself "
        "(everything under the current org_id/workspace_id); pass 'path' to list one subfolder's "
        "immediate contents, e.g. path='docs/features/my-feature' to see that feature's files. "
        "Omit 'path' to list the workspace root. Returns 'folders' (immediate subfolder names) "
        "and 'files' (immediate files, with their full path) at that level — not the whole tree "
        "at once. Call again with a returned folder path to descend further. Pass 'feature_id' "
        "to scope the whole listing to one feature instead of the entire workspace."
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
                    "Optional — scope the listing to this feature's document folder instead of "
                    "the whole workspace."
                ),
            },
            "path": {
                "type": "string",
                "description": (
                    "Folder to list the immediate contents of, relative to the workspace root "
                    "(e.g. 'docs/features/my-feature'). Omit or pass '' for the workspace root."
                ),
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}


def _immediate_children(documents: List[Dict[str, Any]], prefix: str) -> Dict[str, Any]:
    """Split *documents* into the folders/files directly under *prefix*.

    *prefix* is normalized to have no leading slash and, when non-empty, a
    single trailing slash. A document belongs at this level only when its
    path starts with *prefix*; the remainder up to the next '/' is either a
    file name (no further '/') or a subfolder name (has a further '/').
    """
    prefix = prefix.strip("/")
    norm_prefix = f"{prefix}/" if prefix else ""

    folders: Dict[str, None] = {}
    files: List[Dict[str, Any]] = []
    for doc in documents:
        path = doc.get("path") or ""
        if norm_prefix and not path.startswith(norm_prefix):
            continue
        remainder = path[len(norm_prefix):]
        if not remainder:
            continue
        if "/" in remainder:
            folders[remainder.split("/", 1)[0]] = None
        else:
            files.append(
                {
                    "id": doc.get("id"),
                    "path": path,
                    "feature_id": doc.get("feature_id"),
                    "current_version_id": doc.get("current_version_id"),
                    "created_at": doc.get("created_at"),
                }
            )
    return {"folders": sorted(folders), "files": files}


def handle(
    workspace_id: str = "",
    feature_id: str = "",
    path: str = "",
    **_: Any,
) -> Dict[str, Any]:
    from ..context import get_org_id, get_user_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    caller_user_id = get_user_id()
    caller_org_id = get_org_id()

    if not wid:
        return {"ok": False, "error": "workspace_id is required but was not provided and no context is set."}

    try:
        _validate_id(wid, "workspace_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if feature_id:
        try:
            _validate_id(feature_id, "feature_id")
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}

    try:
        result = _list_documents(wid, feature_id, user_id=caller_user_id, org_id=caller_org_id)
    except StorageServiceError as exc:
        logger.warning("list_documents: storage-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("list_documents: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}

    children = _immediate_children(result.get("documents", []), path)
    return {
        "ok": True,
        "path": path.strip("/"),
        "folders": children["folders"],
        "files": children["files"],
    }
