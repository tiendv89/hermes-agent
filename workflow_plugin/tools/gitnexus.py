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
        "calls X', or 'what breaks if I change X' — prefer it over guessing about code. "
        "Always pass a non-empty 'query' (the symbol name or natural-language question), "
        "e.g. query='AIAgent', tool='query'."
    ),
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "The lookup target — a symbol/function/class name for query/context/impact, "
                "a natural-language question for group_query, or a comma-separated file list "
                "for detect_changes. Required for every tool except list_repos."
            ),
        },
        "tool": {
            "type": "string",
            "enum": ["query", "context", "impact", "detect_changes", "list_repos", "group_query"],
            "default": "query",
            "description": (
                "GitNexus operation: query (find a symbol) | context (callers/callees of a symbol) | "
                "impact (blast radius of changing a symbol) | detect_changes (symbols affected by a "
                "file list) | list_repos (no query needed) | group_query (cross-repo flow)."
            ),
        },
    },
    "required": ["query"],
    "additionalProperties": False,
}


def check_available(**_: Any) -> bool:
    """Return True only when GITNEXUS_MCP_URL is configured."""
    return bool(os.environ.get("GITNEXUS_MCP_URL", "").strip())


def _build_arguments(tool: str, query: str) -> Dict[str, Any]:
    """Map the wrapper's single ``query`` input to the per-tool argument shape.

    The GitNexus MCP tools each take a differently-named argument (confirmed
    against git-nexus: the ``query`` tool takes ``q``, not ``query``):
        query / group_query → {"q": ...}
        context / impact     → {"symbol": ...}
        detect_changes       → {"files": [...]}   (comma/space-separated input)
        list_repos           → {}
    """
    if tool == "list_repos":
        return {}
    if tool in ("context", "impact"):
        return {"symbol": query}
    if tool == "detect_changes":
        files = [f.strip() for f in query.replace(",", " ").split() if f.strip()]
        return {"files": files}
    # query and group_query both take `q`.
    return {"q": query}


async def handle(query: str = "", tool: str = "query", **_: Any) -> Dict[str, Any]:
    if not query and tool != "list_repos":
        return {"ok": False, "error": "query is required for GitNexus tool %r." % tool}
    url = os.environ.get("GITNEXUS_MCP_URL", "").strip()
    if not url:
        return {"ok": False, "error": "GITNEXUS_MCP_URL is not configured."}
    try:
        results = await call_mcp_tool(url, tool, _build_arguments(tool, query))
        return {"ok": True, "results": results}
    except Exception as exc:
        logger.warning("workflow_query_gitnexus failed: %s", exc)
        return {"ok": False, "error": str(exc)}
