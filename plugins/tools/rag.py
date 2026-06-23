"""query_rag tool — async semantic search via the RAG MCP server."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from ..mcp_client import call_mcp_tool, coerce_text

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "description": (
        "Semantic search over indexed workspace documents — past specs, technical designs, "
        "task logs, skills, PR descriptions. Call this to recall prior decisions or find "
        "'has anything similar been done before' across feature history. Always pass a "
        "non-empty 'query' describing what to recall, e.g. query='auth flow technical design'. "
        "workspace_id is filled from the current session context automatically."
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
            "top_k": {
                "type": "integer",
                "default": 5,
                "description": "Number of ranked chunks to return.",
            },
            "source_types": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional filter: skill, task_log, product_spec, technical_design, readme, "
                    "claude_md, pr_description."
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
    top_k: int = 5,
    source_types: Any = None,
    **_: Any,
) -> Dict[str, Any]:
    from ..context import get_workspace_id

    # The model may pass query as a structured object over the MCP path; the RAG
    # server validates it as a string, so coerce before forwarding.
    query = coerce_text(query)
    if not query:
        return {"ok": False, "error": "query is required."}
    wid = coerce_text(workspace_id) or get_workspace_id()
    if not wid:
        return {"ok": False, "error": "workspace_id is required but was not provided and no workspace context is set."}
    url = os.environ.get("RAG_MCP_URL", "").strip()
    if not url:
        return {"ok": False, "error": "RAG_MCP_URL is not configured."}
    arguments: Dict[str, Any] = {"query": query, "workspace_id": wid, "top_k": top_k}
    if source_types:
        arguments["source_types"] = source_types
    # Record the context-gathering attempt so the design-write gate is satisfied
    # (see artifacts.py). Marking on attempt — not only on hits — is intentional:
    # a query against an empty/unavailable index still discharges the "gather
    # context first" requirement.
    from ..context import mark_context_gathered

    mark_context_gathered()
    try:
        results = await call_mcp_tool(url, "rag_query", arguments)
        return {"ok": True, "results": results}
    except Exception as exc:
        logger.warning("query_rag failed: %s", exc)
        return {"ok": False, "error": str(exc)}
