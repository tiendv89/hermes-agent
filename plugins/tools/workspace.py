"""get_workspace_context tool — reads workspace metadata from workflow-backend,
plus a best-effort documents list and summary from storage-service/RAG.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional, Tuple

from plugins.clients.storage_service_client import (
    StorageServiceError,
    list_documents,
    read_document_content,
)
from src.services.workflow_backend_client import get_workspace_context, run_async

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "description": (
        "Read a workspace's context — its repos, roles, environments and workflow "
        "settings from workflow-backend, plus its workspace-root documents from "
        "storage-service and a best-effort summary (a CLAUDE.md/README/overview "
        "document if one exists at the workspace root, else a RAG search over "
        "indexed docs). Use this to learn which repos, stacks, and existing "
        "documentation a feature spans before designing or breaking down work."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier (slug or UUID). Omit to use the current workspace from context.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}

# Workspace-root filenames checked (in order) for a ready-made summary before
# falling back to a RAG search — see _find_summary_document.
_SUMMARY_FILENAMES = ("CLAUDE.md", "README.md", "overview.md", "summary.md")


def _find_summary_document(
    documents: List[Dict[str, Any]], wid: str, user_id: str, org_id: str
) -> Tuple[Optional[str], str]:
    """Return (path, content) for the first workspace-root doc matching a
    known summary filename, or (None, "") if none exist/are readable."""
    root_docs = {
        (d.get("path") or "").lower(): d.get("path")
        for d in documents
        if not d.get("feature_id")
    }
    for name in _SUMMARY_FILENAMES:
        path = root_docs.get(name.lower())
        if not path:
            continue
        try:
            doc = read_document_content(wid, "", path, user_id=user_id, org_id=org_id)
        except StorageServiceError as exc:
            logger.debug("get_workspace_context: failed to read summary doc %s: %s", path, exc)
            continue
        if doc.get("content"):
            return path, doc["content"]
    return None, ""


async def _rag_overview_snippets(wid: str, org_id: str) -> List[Dict[str, Any]]:
    """Best-effort RAG search used only when no static summary doc exists."""
    url = os.environ.get("RAG_MCP_URL", "").strip()
    if not url or not org_id:
        return []
    from plugins.clients.mcp_client import call_mcp_tool

    try:
        return await call_mcp_tool(
            url,
            "rag_query",
            {
                "query": "workspace overview and architecture summary",
                "organization_id": org_id,
                "workspace_id": wid,
                "top_k": 3,
            },
            workspace_id=wid,
            organization_id=org_id,
            api_key=os.environ.get("RAG_MCP_TOKEN", ""),
        )
    except Exception as exc:
        logger.debug("get_workspace_context: RAG overview query failed: %s", exc)
        return []


def handle(workspace_id: str = "", **_: Any) -> Dict[str, Any]:
    from ..context import get_org_id, get_user_id, get_workspace_id

    wid = workspace_id or get_workspace_id()
    if not wid:
        return {"ok": False, "error": "workspace_id is required but was not provided and no workspace context is set."}

    # Capture identity on this (calling) thread — the coroutine may run on
    # a different thread via run_async, where thread-local context is unset.
    user_id = get_user_id()
    org_id = get_org_id()
    try:
        workspace = run_async(get_workspace_context(wid, user_id=user_id, org_id=org_id))
    except Exception as exc:
        logger.warning("get_workspace_context failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    if not org_id:
        from src.services.workflow_backend_client import get_workspace_organization_id

        try:
            org_id = run_async(get_workspace_organization_id(wid, user_id=user_id, org_id=org_id)) or ""
        except Exception as exc:
            logger.debug("get_workspace_context: org_id lookup failed: %s", exc)
            org_id = ""

    try:
        documents = list_documents(wid, "", user_id=user_id, org_id=org_id).get("documents", [])
    except Exception as exc:
        logger.debug("get_workspace_context: list_documents failed: %s", exc)
        documents = []

    workspace["documents"] = [
        {"id": d.get("id"), "path": d.get("path")} for d in documents if not d.get("feature_id")
    ]

    summary_path, summary_content = _find_summary_document(documents, wid, user_id, org_id)
    if summary_content:
        workspace["summary"] = summary_content
        workspace["summary_source"] = summary_path
    else:
        snippets = run_async(_rag_overview_snippets(wid, org_id))
        workspace["summary"] = snippets
        workspace["summary_source"] = "rag" if snippets else None

    return {"ok": True, "workspace": workspace}
