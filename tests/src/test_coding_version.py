"""Unit tests for GET /api/v1/coding/version — per-IDE version info."""

from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Stub ONLY missing third-party modules.
# Do NOT stub packages that exist in the project source tree (plugins, src,
# profiles, etc.) — only stub what `pip` didn't install.
# ---------------------------------------------------------------------------


def _stub_mod(name: str) -> types.ModuleType:
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return sys.modules[name]


# mcp + submodules (imported by plugins/clients/mcp_client.py)
_mcp = _stub_mod("mcp")
_mcp.ClientSession = MagicMock  # type: ignore[attr-defined]
_mcp_client = _stub_mod("mcp.client")
_mcp_client_sse = _stub_mod("mcp.client.sse")
_mcp_client_sse.sse_client = MagicMock  # type: ignore[attr-defined]
_mcp.client = _mcp_client  # type: ignore[attr-defined]

# hermes_cli + submodules (imported by src/app.py, profiles/*/setup.py)
_hermes_cli = _stub_mod("hermes_cli")
_hermes_cli_plugins = _stub_mod("hermes_cli.plugins")
_hermes_cli_plugins.PluginContext = MagicMock  # type: ignore[attr-defined]
_hermes_cli_plugins.PluginManifest = MagicMock  # type: ignore[attr-defined]
_hermes_cli_plugins.get_plugin_manager = MagicMock  # type: ignore[attr-defined]
_hermes_cli.plugins = _hermes_cli_plugins  # type: ignore[attr-defined]

# hermes_state (imported from src.db modules)
_hermes_state = _stub_mod("hermes_state")
_hermes_state.SessionDB = MagicMock  # type: ignore[attr-defined]

# run_agent (imported by agent_dispatch)
_run_agent = _stub_mod("run_agent")
_run_agent.AIAgent = MagicMock  # type: ignore[attr-defined]

# tools (imported by agent_dispatch for clarify_gateway)
_tools = _stub_mod("tools")
_tools.clarify_gateway = MagicMock  # type: ignore[attr-defined]

# hermes_constants (imported by agent_dispatch)
_hc = _stub_mod("hermes_constants")
_hc.get_hermes_home = MagicMock(return_value=REPO_ROOT)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Remove coding version env vars between tests."""
    for key in list(os.environ):
        if key.startswith("CODING_"):
            monkeypatch.delenv(key, raising=False)
    yield


def _make_app():
    """Minimal FastAPI app with the coding profile router."""
    from fastapi import FastAPI

    from profiles.coding.setup import build_router

    app = FastAPI()
    app.include_router(build_router(), prefix="/api/v1")
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_version_returns_default_json_shape():
    """GET /api/v1/coding/version returns the expected JSON shape with defaults."""
    app = _make_app()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/api/v1/coding/version")

    assert resp.status_code == 200
    data = resp.json()

    # Top-level keys.
    assert "vscode" in data
    assert "jetbrains" in data

    # VS Code fields.
    vscode = data["vscode"]
    assert vscode["min_version"] == "1.0.0"
    assert vscode["recommended_version"] == "1.0.0"
    assert "marketplace.visualstudio.com" in vscode["marketplace_url"]
    assert vscode["deprecation_notice"] is None

    # JetBrains fields.
    jb = data["jetbrains"]
    assert jb["min_version"] == "1.0.0"
    assert jb["recommended_version"] == "1.0.0"
    assert "plugins.jetbrains.com" in jb["marketplace_url"]
    assert jb["deprecation_notice"] is None


@pytest.mark.asyncio
async def test_version_env_var_overrides(monkeypatch):
    """Env vars override the default version values."""
    monkeypatch.setenv("CODING_VSCODE_MIN_VERSION", "2.0.0")
    monkeypatch.setenv("CODING_VSCODE_RECOMMENDED_VERSION", "2.5.0")
    monkeypatch.setenv(
        "CODING_VSCODE_MARKETPLACE_URL", "https://example.com/vscode"
    )
    monkeypatch.setenv(
        "CODING_VSCODE_DEPRECATION_NOTICE", "v1.x deprecated on 2026-09-01"
    )
    monkeypatch.setenv("CODING_JETBRAINS_MIN_VERSION", "2.1.0")
    monkeypatch.setenv("CODING_JETBRAINS_RECOMMENDED_VERSION", "2.6.0")
    monkeypatch.setenv(
        "CODING_JETBRAINS_MARKETPLACE_URL", "https://example.com/jb"
    )
    monkeypatch.setenv(
        "CODING_JETBRAINS_DEPRECATION_NOTICE", "v1.x deprecated on 2026-10-01"
    )

    app = _make_app()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/api/v1/coding/version")

    assert resp.status_code == 200
    data = resp.json()

    assert data["vscode"]["min_version"] == "2.0.0"
    assert data["vscode"]["recommended_version"] == "2.5.0"
    assert data["vscode"]["marketplace_url"] == "https://example.com/vscode"
    assert (
        data["vscode"]["deprecation_notice"] == "v1.x deprecated on 2026-09-01"
    )

    assert data["jetbrains"]["min_version"] == "2.1.0"
    assert data["jetbrains"]["recommended_version"] == "2.6.0"
    assert data["jetbrains"]["marketplace_url"] == "https://example.com/jb"
    assert (
        data["jetbrains"]["deprecation_notice"]
        == "v1.x deprecated on 2026-10-01"
    )


@pytest.mark.asyncio
async def test_version_no_auth_required():
    """The version endpoint is public — no Authorization header needed."""
    app = _make_app()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get("/api/v1/coding/version")

    assert resp.status_code == 200
