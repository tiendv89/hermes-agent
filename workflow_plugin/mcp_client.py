"""Shared async MCP SSE client helper for workflow plugin tools."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse, urlunparse

from mcp import ClientSession
from mcp.client.sse import sse_client


def _content_to_dict(c: Any) -> dict:
    """Convert MCP content item (TextContent, EmbeddedResource, etc.) to a plain dict."""
    if hasattr(c, "text"):
        return {"type": "text", "text": c.text}
    if hasattr(c, "model_dump"):
        return c.model_dump()
    return {"type": "unknown", "value": str(c)}


def _sse_endpoint(base_url: str) -> str:
    """Resolve the SSE endpoint URL from a configured base URL.

    The RAG and GitNexus MCP servers serve their SSE stream at ``/sse`` (see
    rag-service / git-nexus ``server.py``). Operators typically configure the
    bare host (e.g. ``https://rag.tempestdev.xyz``), so default the path to
    ``/sse`` when none is given. A URL that already carries a path is left
    untouched so explicit overrides keep working.
    """
    parsed = urlparse(base_url.strip())
    if parsed.path in ("", "/"):
        parsed = parsed._replace(path="/sse")
    return urlunparse(parsed)


async def call_mcp_tool(base_url: str, tool: str, arguments: dict) -> list[dict]:
    """Connect to an MCP SSE server, run a single tool call, return content as plain dicts.

    base_url is the service's base or SSE endpoint, e.g. https://rag.tempestdev.xyz
    (the ``/sse`` path is appended automatically) or http://gitnexus:8002/sse.
    """
    async with sse_client(_sse_endpoint(base_url)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            return [_content_to_dict(c) for c in result.content]
