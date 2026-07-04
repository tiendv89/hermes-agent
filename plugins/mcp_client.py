"""Shared async MCP SSE client helper for workflow plugin tools."""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any
from urllib.parse import urlparse, urlunparse

from mcp import ClientSession
from mcp.client.sse import sse_client

_MCP_TIMEOUT_SECONDS = float(os.environ.get("MCP_CALL_TIMEOUT_SECONDS", "60"))


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
    while (
        isinstance(exc, BaseExceptionGroup) and exc.exceptions and id(exc) not in seen
    ):
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


def _sse_endpoint(base_url: str, workspace_id: str = "") -> str:
    """Resolve the SSE endpoint URL from a configured base URL.

    When *workspace_id* is provided the endpoint is scoped to that workspace:
    ``…/ws/<workspace_id>/sse``.  Without a workspace_id the legacy ``/sse``
    path is used, preserving backward compatibility for single-workspace
    deployments.

    Operators typically configure the bare host
    (e.g. ``https://rag.tempestdev.xyz``).  An existing path in the URL is
    replaced when workspace_id is given so that explicit per-workspace overrides
    in the env var do not silently bypass scoping.
    """
    parsed = urlparse(base_url.strip())
    if workspace_id:
        parsed = parsed._replace(path=f"/ws/{workspace_id}/sse")
    elif parsed.path in ("", "/"):
        parsed = parsed._replace(path="/sse")
    return urlunparse(parsed)


async def call_mcp_tool(
    base_url: str,
    tool: str,
    arguments: dict,
    workspace_id: str = "",
) -> list[dict]:
    """Connect to an MCP SSE server, run a single tool call, return content as plain dicts.

    base_url is the service's base or SSE endpoint, e.g. https://rag.tempestdev.xyz
    (the ``/sse`` path is appended automatically) or http://gitnexus:8002/sse.

    When *workspace_id* is supplied the connection targets the workspace-scoped
    endpoint ``…/ws/<workspace_id>/sse`` so all tools on that connection are
    automatically scoped to that workspace — no per-tool argument needed.
    """
    endpoint = _sse_endpoint(base_url, workspace_id)

    async def _run() -> list[dict]:
        async with sse_client(endpoint) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, arguments)
                return [_content_to_dict(c) for c in result.content]

    try:
        return await asyncio.wait_for(_run(), timeout=_MCP_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        raise MCPCallError(
            f"MCP {tool!r} call to {endpoint} timed out after {_MCP_TIMEOUT_SECONDS:.0f}s "
            f"— the server is unreachable from this host (check network egress, the "
            f"configured URL/scheme, and that the server is up)."
        ) from exc
    except BaseExceptionGroup as eg:
        leaf = _unwrap_exception(eg)
        raise MCPCallError(f"MCP {tool!r} call to {endpoint} failed: {leaf}") from leaf
    except MCPCallError:
        raise
    except Exception as exc:
        raise MCPCallError(
            f"MCP {tool!r} call to {endpoint} failed: {type(exc).__name__}: {exc}"
        ) from exc
