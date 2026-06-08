"""Shared async MCP SSE client helper for workflow plugin tools."""

from __future__ import annotations

from typing import Any

from mcp import ClientSession
from mcp.client.sse import sse_client


def _content_to_dict(c: Any) -> dict:
    """Convert MCP content item (TextContent, EmbeddedResource, etc.) to a plain dict."""
    if hasattr(c, "text"):
        return {"type": "text", "text": c.text}
    if hasattr(c, "model_dump"):
        return c.model_dump()
    return {"type": "unknown", "value": str(c)}


async def call_mcp_tool(base_url: str, tool: str, arguments: dict) -> list[dict]:
    """Connect to an MCP SSE server, run a single tool call, return content as plain dicts.

    base_url is the service's SSE endpoint, e.g. http://gitnexus:8002/sse
    """
    async with sse_client(base_url) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, arguments)
            return [_content_to_dict(c) for c in result.content]
