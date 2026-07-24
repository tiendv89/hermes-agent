"""Tests for move_feature_status tool.

Covers:
  - backlog feature: moves to in_design via update_feature_status, returns the
    review-before-final-design next_action
  - non-backlog feature: safe no-op, no DB write
  - missing ids: error
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


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


_WORKSPACE_ID = "ws-test"
_FEATURE_ID = "my-feature"


def _import_handle():
    # Install a lightweight `plugins` package stub so loading move_feature.py
    # directly (with its `from ..validation import ...`) does not trigger a full
    # real plugins/__init__.py execution mid-load. Mirrors test_approve.py.
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(REPO_ROOT / "plugins")]
        pkg.__package__ = "plugins"
        sys.modules["plugins"] = pkg

    spec = importlib.util.spec_from_file_location(
        "plugins.tools.move_feature",
        REPO_ROOT / "plugins" / "tools" / "move_feature.py",
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "plugins.tools"
    sys.modules["plugins.tools.move_feature"] = mod
    spec.loader.exec_module(mod)
    return mod.handle


def _detail(status: str) -> dict:
    return {"feature_name": _FEATURE_ID, "stage": "product_spec", "status": status}


def _run(status: str):
    handle = _import_handle()
    update_mock = AsyncMock()
    with (
        patch(
            "src.services.workflow_backend_client.get_feature_detail",
            AsyncMock(return_value=_detail(status)),
        ),
        patch("src.services.workflow_backend_client.update_feature_status", update_mock),
        patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
        patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
        patch("plugins.context.get_user_id", return_value="user-1"),
        patch("plugins.context.get_org_id", return_value="org-1"),
    ):
        result = handle()
    return result, update_mock


class TestMoveFeatureStatus:
    def test_backlog_moves_to_in_design(self):
        result, update_mock = _run("backlog")
        assert result["ok"] is True
        assert result["action"] == "moved"
        assert result["feature_status"] == "in_design"
        update_mock.assert_called_once()
        # (workspace_id, feature_id, feature_status, actor) positional args.
        call = update_mock.call_args
        assert call.args[0] == _WORKSPACE_ID
        assert call.args[1] == _FEATURE_ID
        assert call.args[2] == "in_design"

    def test_backlog_returns_review_next_action(self):
        result, _ = _run("backlog")
        assert "Final Design" in result["next_action"]
        assert "review" in result["next_action"].lower()

    def test_non_backlog_is_noop(self):
        result, update_mock = _run("in_design")
        assert result["ok"] is True
        assert result["action"] == "noop"
        assert result["feature_status"] == "in_design"
        update_mock.assert_not_called()

    def test_missing_ids_returns_error(self):
        handle = _import_handle()
        with (
            patch("plugins.context.get_feature_id", return_value=""),
            patch("plugins.context.get_workspace_id", return_value=""),
            patch("plugins.context.get_user_id", return_value=""),
            patch("plugins.context.get_org_id", return_value=""),
        ):
            result = handle(workspace_id="", feature_id="")
        assert result["ok"] is False
        assert "required" in result["error"]
