"""workflow_query_gitnexus tool — async passthrough to the GitNexus MCP server."""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from ..mcp_client import call_mcp_tool

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "description": (
        "Query the code-structure index (GitNexus) for symbol definitions, call graphs, "
        "and impact/blast-radius. Call this before answering 'where is X defined', 'what "
        "calls X', or 'what breaks if I change X' — prefer it over guessing about code."
    ),
    "properties": {
        "query": {
            "type": "string",
            "description": "Natural-language or structured query.",
        },
        "tool": {
            "type": "string",
            "default": "query",
            "description": "GitNexus tool: query | context | impact | detect_changes | list_repos | group_query",
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def check_available(**_: Any) -> bool:
    """Return True only when GITNEXUS_MCP_URL is configured."""
    return bool(os.environ.get("GITNEXUS_MCP_URL", "").strip())


async def handle(query: str, tool: str = "query", **_: Any) -> Dict[str, Any]:
    url = os.environ["GITNEXUS_MCP_URL"]
    try:
        results = await call_mcp_tool(url, tool, {"query": query})
        return {"ok": True, "results": results}
    except Exception as exc:
        logger.warning("workflow_query_gitnexus failed: %s", exc)
        return {"ok": False, "error": str(exc)}
