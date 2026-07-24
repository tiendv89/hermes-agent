"""Tests for T3: request_approval tool + GET /tools.

Covers:
  - request_approval: returns payload, writes nothing
  - request_approval: missing feature_id returns error
  - request_approval: invalid stage returns error
  - request_approval: review_status read from workflow-backend's feature detail
  - GET /tools: returns live registry honoring check_fn
  - GET /tools: gated tools (check_fn returns False) are excluded
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Module cleanup fixture (same as other test files in this dir)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    keys = [k for k in sys.modules if k.startswith(("plugins", "src"))]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith(("plugins", "src"))]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKSPACE_ID = "ws-test"
_FEATURE_ID = "my-feature"
_ACTOR = "user-42"


def _make_feature_detail(stages: dict | None = None):
    return {
        "feature_name": _FEATURE_ID,
        "stage": "product_spec",
        "status": "in_design",
        "stages": stages if stages is not None else {},
    }


# ---------------------------------------------------------------------------
# request_approval — read-only tool
# ---------------------------------------------------------------------------


class TestWorkflowRequestApproval:
    def _import_handle(self):
        """Import approval.handle without triggering the shadow-package issue."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plugins.tools.approval",
            REPO_ROOT / "plugins" / "tools" / "approval.py",
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "plugins.tools"
        sys.modules["plugins.tools.approval"] = mod
        spec.loader.exec_module(mod)
        return mod.handle

    def test_returns_approval_request_payload(self, monkeypatch):
        handle = self._import_handle()
        stages = {"product_spec": {"review_status": "draft"}}

        with (
            patch("src.services.workflow_backend_client.get_feature_detail", AsyncMock(return_value=_make_feature_detail(stages))),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
        ):
            result = handle(stage="product_spec")

        assert result["ok"] is True
        assert result["approval_request"]["feature_id"] == _FEATURE_ID
        assert result["approval_request"]["stage"] == "product_spec"
        assert result["approval_request"]["review_status"] == "draft"

    def test_missing_feature_id_returns_error(self, monkeypatch):
        handle = self._import_handle()

        with patch("plugins.context.get_feature_id", return_value=""):
            result = handle(stage="product_spec", feature_id="")

        assert result["ok"] is False
        assert "feature_id" in result["error"]

    def test_invalid_stage_returns_error(self, monkeypatch):
        handle = self._import_handle()

        with patch("plugins.context.get_feature_id", return_value=_FEATURE_ID):
            result = handle(stage="nonexistent_stage", feature_id=_FEATURE_ID)

        assert result["ok"] is False
        assert "stage" in result["error"].lower()

    def test_explicit_feature_id_overrides_context(self, monkeypatch):
        handle = self._import_handle()
        stages = {"product_spec": {"review_status": "draft"}}

        with (
            patch("src.services.workflow_backend_client.get_feature_detail", AsyncMock(return_value=_make_feature_detail(stages))) as mock_detail,
            patch("plugins.context.get_feature_id", return_value="context-feature"),
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
        ):
            result = handle(stage="product_spec", feature_id="explicit-feature")

        assert result["ok"] is True
        assert result["approval_request"]["feature_id"] == "explicit-feature"
        # Verify the lookup was made for the explicit feature, not the context one.
        call_args = mock_detail.call_args
        assert "explicit-feature" in call_args[0]

    def test_review_status_uses_approved_from_workflow_backend(self, monkeypatch):
        handle = self._import_handle()
        stages = {"product_spec": {"review_status": "approved"}}

        with (
            patch("src.services.workflow_backend_client.get_feature_detail", AsyncMock(return_value=_make_feature_detail(stages))),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
        ):
            result = handle(stage="product_spec", feature_id=_FEATURE_ID)

        assert result["ok"] is True
        assert result["approval_request"]["review_status"] == "approved"

    def test_missing_workspace_id_returns_unknown(self, monkeypatch):
        handle = self._import_handle()

        with (
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_workspace_id", return_value=""),
        ):
            result = handle(stage="product_spec", feature_id=_FEATURE_ID)

        assert result["ok"] is True
        assert result["approval_request"]["review_status"] == "unknown"

    def test_stage_absent_from_stages_returns_draft(self, monkeypatch):
        handle = self._import_handle()

        with (
            patch("src.services.workflow_backend_client.get_feature_detail", AsyncMock(return_value=_make_feature_detail({}))),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
        ):
            result = handle(stage="product_spec", feature_id=_FEATURE_ID)

        assert result["ok"] is True
        assert result["approval_request"]["review_status"] == "draft"


# ---------------------------------------------------------------------------
# GET /tools — live registry list honouring check_fn
# ---------------------------------------------------------------------------


class TestGetToolsEndpoint:
    """Test the GET /tools route via the FastAPI test client."""

    def _build_client(self, monkeypatch):
        """Build a TestClient for the router module, bypassing DB setup."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.api.identity import Identity, require_identity

        app = FastAPI()
        from src.api import router as router_mod
        app.include_router(router_mod.router, prefix="/api/v1")
        # Override the dependency so calls don't touch the DB.
        app.dependency_overrides[require_identity] = lambda: Identity(user_id="test-user")
        return TestClient(app)

    def test_returns_tools_json(self, monkeypatch):
        monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
        monkeypatch.delenv("RAG_MCP_URL", raising=False)
        # list_tools_endpoint reads src.tool_setup._WORKFLOW_TOOLS/_CODING_TOOLS
        # directly (not plugins._TOOLS, which only reflects whichever
        # profile's register() call happened to run last at startup — see
        # test_tools_endpoint.py's own module docstring for the full story).
        # No `source` param defaults to _WORKFLOW_TOOLS.
        fake_tools = (
            {"name": "tool_alpha", "schema": {"description": "Alpha tool."}, "check_fn": None},
            {"name": "tool_beta", "schema": {"description": "Beta tool."}, "check_fn": lambda: True},
        )
        with patch("src.tool_setup._WORKFLOW_TOOLS", fake_tools):
            client = self._build_client(monkeypatch)
            resp = client.get("/api/v1/tools")

        assert resp.status_code == 200
        data = resp.json()
        assert "tools" in data
        names = [t["name"] for t in data["tools"]]
        assert "tool_alpha" in names
        assert "tool_beta" in names

    def test_gated_tool_excluded(self, monkeypatch):
        """A tool whose check_fn returns False must not appear in the list."""
        fake_tools = (
            {"name": "always_on", "schema": {"description": "Always available."}, "check_fn": None},
            {"name": "gated_off", "schema": {"description": "Gated."}, "check_fn": lambda: False},
        )
        with patch("src.tool_setup._WORKFLOW_TOOLS", fake_tools):
            client = self._build_client(monkeypatch)
            resp = client.get("/api/v1/tools")

        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["tools"]]
        assert "always_on" in names
        assert "gated_off" not in names

    def test_tool_description_from_schema(self, monkeypatch):
        fake_tools = (
            {"name": "described", "schema": {"description": "My tool description."}, "check_fn": None},
        )
        with patch("src.tool_setup._WORKFLOW_TOOLS", fake_tools):
            client = self._build_client(monkeypatch)
            resp = client.get("/api/v1/tools")

        tools = resp.json()["tools"]
        assert tools[0]["description"] == "My tool description."

    def test_missing_schema_description_falls_back_to_empty_string(self, monkeypatch):
        fake_tools = (
            {"name": "nodesc", "schema": {}, "check_fn": None},
        )
        with patch("src.tool_setup._WORKFLOW_TOOLS", fake_tools):
            client = self._build_client(monkeypatch)
            resp = client.get("/api/v1/tools")

        tools = resp.json()["tools"]
        assert tools[0]["description"] == ""

    def test_request_approval_in_live_registry(self, monkeypatch):
        """When workflow-backend is configured, request_approval must appear."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        # No `source` param already gets the real _WORKFLOW_TOOLS list by
        # default — no patching needed (unlike before the T2/merged-process
        # fix, which required manually repointing plugins._TOOLS at it).
        client = self._build_client(monkeypatch)
        resp = client.get("/api/v1/tools")
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["tools"]]
        assert "request_approval" in names

    def test_skills_listed_for_frontend(self, monkeypatch):
        """GET /tools also returns the loadable skills, typed technical/workflow."""
        client = self._build_client(monkeypatch)
        resp = client.get("/api/v1/tools")
        assert resp.status_code == 200
        data = resp.json()
        assert "skills" in data

        by_name = {s["name"]: s for s in data["skills"]}
        # A technical (knowledge) skill shows up, tagged with the right type.
        # There are no workflow skills anymore — that workflow is implemented
        # as Python tools instead of markdown instructions.
        assert by_name["python-best-practices"]["type"] == "technical"
        assert all(s["type"] == "technical" for s in data["skills"])
        # Every skill carries a non-empty description for the picker.
        assert all(s["description"] for s in data["skills"])

    def test_request_approval_excluded_when_backend_unset(self, monkeypatch):
        """When the workflow backend is not configured, request_approval is excluded."""
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
        client = self._build_client(monkeypatch)
        resp = client.get("/api/v1/tools")
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["tools"]]
        assert "request_approval" not in names

