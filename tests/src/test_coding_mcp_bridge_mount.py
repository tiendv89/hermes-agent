"""Integration tests for the per-session MCP bridge mount in src/app.py.

Exercises a real MCP initialize + tools/list round trip through the ASGI
Mount (not just the coding_bridge_server module in isolation), since the
mount's path/root_path rewriting and the streamable-http session manager's
task-lifecycle management are exactly what a unit test of
coding_bridge_server.py alone can't catch — see test_coding_bridge_server.py
for the tool-handler-level tests.

Coverage:
  - a real initialize + tools/list handshake succeeds through the mount
  - a request with no session_id segment 404s cleanly instead of crashing
  - discard() tears a session down; a later request for the same session_id
    builds a fresh app (proving the old one was actually released)
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from src.app import _SessionScopedMCPBridge

BASE_URL = "http://127.0.0.1"
INIT_REQ = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test-client", "version": "0.0.1"},
    },
}
SSE_HEADERS = {"Accept": "application/json, text/event-stream"}


@pytest.mark.asyncio
async def test_initialize_and_list_tools_round_trip():
    bridge = _SessionScopedMCPBridge()
    transport = ASGITransport(app=bridge)
    try:
        async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
            r = await client.post(
                "/sess-mount-1/mcp", json=INIT_REQ, headers=SSE_HEADERS
            )
            assert r.status_code == 200
            session_hdr = r.headers.get("mcp-session-id")
            assert session_hdr

            list_req = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
            r2 = await client.post(
                "/sess-mount-1/mcp",
                json=list_req,
                headers={**SSE_HEADERS, "mcp-session-id": session_hdr},
            )
            assert r2.status_code == 200
            assert "read_file" in r2.text
            assert "git_commit" in r2.text
    finally:
        await bridge.discard("sess-mount-1")


@pytest.mark.asyncio
async def test_missing_session_id_returns_404():
    bridge = _SessionScopedMCPBridge()
    transport = ASGITransport(app=bridge)
    async with AsyncClient(transport=transport, base_url=BASE_URL) as client:
        r = await client.post("/", json=INIT_REQ, headers=SSE_HEADERS)
        assert r.status_code == 404


@pytest.mark.asyncio
async def test_discard_releases_and_rebuilds_session_app():
    bridge = _SessionScopedMCPBridge()
    try:
        app1 = await bridge._get_or_create("sess-mount-2")
        assert "sess-mount-2" in bridge._apps
        await bridge.discard("sess-mount-2")
        assert "sess-mount-2" not in bridge._apps

        app2 = await bridge._get_or_create("sess-mount-2")
        assert app2 is not app1
    finally:
        await bridge.discard("sess-mount-2")
