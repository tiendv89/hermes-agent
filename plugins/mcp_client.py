"""Shared async MCP SSE client helper for workflow plugin tools."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse, urlunparse

from mcp import ClientSession
from mcp.client.sse import sse_client


def coerce_text(value: Any) -> str:
    """Coerce a tool argument to a plain string.

    Over the MCP path the model frequently passes a structured object (e.g.
    ``{"query": "auth flow"}``) where a string is expected; the downstream MCP
    server then rejects it with a Pydantic "not a valid string" error. Unwrap
    the common shapes and fall back to a JSON dump so we always send a string.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("query", "q", "text", "value", "name"):
            v = value.get(key)
            if isinstance(v, str):
                return v
        return json.dumps(value, ensure_ascii=False)
    if value is None:
        return ""
    return str(value)


def _unwrap_exception(exc: BaseException) -> BaseException:
    """Drill into (Base)ExceptionGroups to the first meaningful leaf exception.

    anyio/mcp surface connection failures as an ExceptionGroup whose ``str()``
    is the unhelpful "unhandled errors in a TaskGroup"; the real cause (a
    ConnectionError, timeout, etc.) is nested inside.
    """
    seen: set[int] = set()
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions and id(exc) not in seen:
        seen.add(id(exc))
        exc = exc.exceptions[0]
    return exc


class MCPCallError(RuntimeError):
    """Raised when an MCP tool call fails, carrying a clean, unwrapped message."""


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
    endpoint = _sse_endpoint(base_url)
    try:
        async with sse_client(endpoint) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                return [_content_to_dict(c) for c in result.content]
    except BaseExceptionGroup as eg:
        # Connection/transport failures arrive as a TaskGroup ExceptionGroup;
        # unwrap it so callers get the real cause instead of "unhandled errors
        # in a TaskGroup".
        leaf = _unwrap_exception(eg)
        raise MCPCallError(f"MCP {tool!r} call to {endpoint} failed: {leaf}") from leaf
