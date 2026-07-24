"""Tests for GET /tools' source-based toolset scoping.

Regression: register_tools() calls plugins.register() twice at startup (once
for _WORKFLOW_TOOLS, once for _CODING_TOOLS, since both profiles now run in
one merged process) — each call unconditionally overwrites the module-level
plugins._TOOLS with whatever it just registered, so that global only ever
reflects whichever call happened to run last (coding), regardless of which
surface (web workflow chat vs. IDE extension) is actually asking. list_tools_
endpoint must read directly from src.tool_setup's two source tuples instead,
picked by the `source` query param, not from plugins._TOOLS.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

REPO_ROOT_PATH = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_PATH))


@pytest.fixture(autouse=True)
def _clean_tool_setup_import():
    """Force a fresh, clean import of src.tool_setup (and everything it pulls
    in — plugins.hooks, plugins.context, ...) before each test in this file.

    Several other files in this suite (test_workflow_plugin.py,
    test_e2e.py, test_session_context_isolation.py, and ~20 more) pop
    plugins.* submodules out of sys.modules to test fresh-import behavior,
    and — depending on test order — can leave a stubbed-out plugins.context
    behind without restoring it. list_tools_endpoint's `from src.tool_setup
    import ...` is a lazy, per-request import (matching this router's
    existing convention), so it's exposed to whatever sys.modules state the
    PREVIOUS test in the process left behind — pre-existing suite-wide
    fragility, not something specific to this file's own tests. In a real
    server process src.tool_setup is imported exactly once at clean startup,
    long before any request hits GET /tools, so this fixture just reproduces
    that guarantee for tests instead of being at the mercy of run order.
    """
    for name in ("src.tool_setup", "plugins.hooks", "plugins.context"):
        sys.modules.pop(name, None)
    import src.tool_setup  # noqa: F401

    yield


@pytest.mark.asyncio
async def test_tools_endpoint_defaults_to_workflow_toolset():
    """No `source` param -> the web workflow chat's own tools (write_product_spec, ...)."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.routers.tools import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/api/v1/tools")

    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["tools"]}
    # suggest_next_actions has no check_fn gate, so it's deterministically
    # present regardless of test-env config (unlike write_product_spec,
    # which is gated behind check_workflow_available — not set up here).
    assert "suggest_next_actions" in names
    assert "search_code" not in names
    assert "coding_read_file" not in names


@pytest.mark.asyncio
async def test_tools_endpoint_coding_ide_source_returns_coding_toolset():
    """source=coding-ide -> the IDE extension's own tools (search_code, ...), not workflow's."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.routers.tools import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/api/v1/tools", params={"source": "coding-ide"})

    assert resp.status_code == 200
    names = {t["name"] for t in resp.json()["tools"]}
    assert "search_code" in names
    assert "suggest_next_actions" not in names


@pytest.mark.asyncio
async def test_tools_endpoint_both_sources_include_shared_tools():
    """A tool pinned toolset="shared" (load_skill) appears for both sources."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.routers.tools import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        workflow_resp = await client.get("/api/v1/tools")
        coding_resp = await client.get("/api/v1/tools", params={"source": "coding-ide"})

    workflow_names = {t["name"] for t in workflow_resp.json()["tools"]}
    coding_names = {t["name"] for t in coding_resp.json()["tools"]}
    assert "load_skill" in workflow_names
    assert "load_skill" in coding_names
