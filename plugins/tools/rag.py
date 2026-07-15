"""query_rag tool — async semantic search via the RAG MCP server."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from plugins.clients.mcp_client import call_mcp_tool, coerce_text

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "description": (
        "Semantic search over indexed workspace documents — product specs, technical designs, "
        "and other docs stored via storage-service. Call this to recall prior decisions or find "
        "'has anything similar been done before' across feature history. Always pass a "
        "non-empty 'query' describing what to recall, e.g. query='auth flow technical design'. "
        "organization_id/workspace_id are filled from the current session context automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Natural-language description of what to recall (required, non-empty).",
            },
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier. Omit to use the current workspace from context.",
            },
            "organization_id": {
                "type": "string",
                "description": "Organization identifier. Omit to use the current organization from context.",
            },
            "top_k": {
                "type": "integer",
                "default": 5,
                "description": "Number of ranked chunks to return.",
            },
            "source_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional filter: product_spec, technical_design, doc.",
            },
            "feature_name": {
                "type": "string",
                "description": (
                    "Optional human-readable feature slug (e.g. 'checkout-flow') to "
                    "restrict results to that feature's documents."
                ),
            },
            "feature_id": {
                "type": "string",
                "description": (
                    "Feature identifier. Omit inside a feature-scoped session (resolved "
                    "from context automatically). In a general-chat session (no current "
                    "feature) pass the feature's id explicitly — e.g. from "
                    "workflow_lookup_feature — so this call counts toward that feature's "
                    "context-gathering gate for write_product_spec/write_technical_design."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def check_available(**_: Any) -> bool:
    """Return True only when RAG_MCP_URL is configured."""
    return bool(os.environ.get("RAG_MCP_URL", "").strip())


async def handle(
    query: str = "",
    workspace_id: str = "",
    organization_id: str = "",
    top_k: int = 5,
    source_types: Any = None,
    feature_name: Any = None,
    feature_id: Any = "",
    **_: Any,
) -> Dict[str, Any]:
    from ..context import get_org_id, get_workspace_id

    # The model may pass query as a structured object over the MCP path; the RAG
    # server validates it as a string, so coerce before forwarding.
    query = coerce_text(query)
    if not query:
        return {"ok": False, "error": "query is required."}
    wid = coerce_text(workspace_id) or get_workspace_id()
    if not wid:
        return {
            "ok": False,
            "error": "workspace_id is required but was not provided and no workspace context is set.",
        }

    org_id = coerce_text(organization_id)
    if not org_id:
        from src.services.workflow_backend_client import get_workspace_organization_id

        try:
            org_id = await get_workspace_organization_id(wid)
        except Exception as exc:
            logger.debug("query_rag: workspace org lookup failed: %s", exc)
            org_id = None
    if not org_id:
        org_id = get_org_id()
    if not org_id:
        return {
            "ok": False,
            "error": "organization_id is required but no organization context is set for this session.",
        }
    url = os.environ.get("RAG_MCP_URL", "").strip()
    if not url:
        return {"ok": False, "error": "RAG_MCP_URL is not configured."}
    arguments: Dict[str, Any] = {
        "query": query,
        "organization_id": org_id,
        "workspace_id": wid,
        "top_k": top_k,
    }
    if source_types:
        arguments["source_types"] = source_types
    feature_name = coerce_text(feature_name)
    if feature_name:
        arguments["feature_name"] = feature_name
    # Record the context-gathering attempt so the design-write gate is satisfied
    # (see artifacts.py). Marking on attempt — not only on hits — is intentional:
    # a query against an empty/unavailable index still discharges the "gather
    # context first" requirement. Pass the explicit feature_id through (falls
    # back to the thread-local current feature when omitted) so this credits
    # the right feature in a general-chat session, which has no current
    # feature set.
    from ..context import mark_context_gathered

    mark_context_gathered(coerce_text(feature_id))
    try:
        results = await call_mcp_tool(
            url, "rag_query", arguments, workspace_id=wid, organization_id=org_id
        )
        return {"ok": True, "results": results}
    except Exception as exc:
        logger.warning("query_rag failed: %s", exc)
        return {
            "ok": False,
            "error": (
                f"{exc} — if this workspace/repo was created recently, RAG "
                "indexing may still be in progress; retry shortly before "
                "concluding no documents exist."
            ),
        }
