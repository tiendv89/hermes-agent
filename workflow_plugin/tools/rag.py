"""workflow_query_rag tool — async semantic search via the RAG MCP server."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from ..mcp_client import call_mcp_tool

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Semantic search over indexed workspace documents — past specs, technical designs, "
        "task logs, skills, PR descriptions. Call this to recall prior decisions or find "
        "'has anything similar been done before' across feature history."
    ),
    "properties": {
        "query": {"type": "string"},
        "workspace_id": {"type": "string"},
        "top_k": {"type": "integer", "default": 5},
    },
    "required": ["query", "workspace_id"],
    "additionalProperties": False,
}


def check_available(**_: Any) -> bool:
    """Return True only when RAG_MCP_URL is configured."""
    return bool(os.environ.get("RAG_MCP_URL", "").strip())


async def handle(
    query: str, workspace_id: str, top_k: int = 5, **_: Any
) -> Dict[str, Any]:
    url = os.environ["RAG_MCP_URL"]
    try:
        results = await call_mcp_tool(
            url,
            "rag_query",
            {"query": query, "workspace_id": workspace_id, "top_k": top_k},
        )
        return {"ok": True, "results": results}
    except Exception as exc:
        logger.warning("workflow_query_rag failed: %s", exc)
        return {"ok": False, "error": str(exc)}
