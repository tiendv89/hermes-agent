"""Tests for the read_workspace_file tool.

Covers:
  - handle: successful read returns content/exists/sha
  - handle: not-found path returns exists=False, ok=True
  - handle: missing path returns ok=False
  - handle: missing workspace_id (no arg, no context) returns ok=False
  - handle: workspace_id falls back to session context
  - handle: invalid workspace_id characters rejected
  - handle: storage-service error surfaces as ok=False
  - handle: always reads with feature_id="" (never leaks a feature scope)
  - _TOOLS: read_workspace_file registered with check_fn
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _load_plugins_init():
    init_path = REPO_ROOT / "plugins" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "plugins",
        init_path,
        submodule_search_locations=[str(REPO_ROOT / "plugins")],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "plugins"
    mod.__path__ = [str(REPO_ROOT / "plugins")]
    sys.modules["plugins"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_modules():
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]


class TestHandleReadWorkspaceFile:
    def test_successful_read_returns_content(self, monkeypatch):
        import plugins.context as ctx

        ctx.set_context("sess-1", "ws-1", "")
        with patch(
            "plugins.tools.read_workspace_file.read_document_content",
            return_value={"content": "package main\n", "version_id": "v1"},
        ):
            from plugins.tools.read_workspace_file import handle

            result = handle(path="tests/api.go")

        assert result["ok"] is True
        assert result["exists"] is True
        assert result["content"] == "package main\n"
        assert result["sha"] == "v1"
        assert result["path"] == "tests/api.go"

    def test_not_found_returns_exists_false(self, monkeypatch):
        import plugins.context as ctx

        ctx.set_context("sess-2", "ws-1", "")
        with patch(
            "plugins.tools.read_workspace_file.read_document_content",
            return_value={"content": "", "version_id": None},
        ):
            from plugins.tools.read_workspace_file import handle

            result = handle(path="does/not/exist.md")

        assert result["ok"] is True
        assert result["exists"] is False
        assert result["content"] == ""

    def test_missing_path_returns_error(self, monkeypatch):
        import plugins.context as ctx

        ctx.set_context("sess-3", "ws-1", "")
        from plugins.tools.read_workspace_file import handle

        result = handle(path="")
        assert result["ok"] is False
        assert "required" in result["error"]

    def test_missing_workspace_id_returns_error(self):
        import plugins.context as ctx

        ctx.set_context("sess-4", "", "")
        from plugins.tools.read_workspace_file import handle

        result = handle(path="tests/api.go")
        assert result["ok"] is False
        assert "workspace_id" in result["error"]

    def test_workspace_id_falls_back_to_context(self, monkeypatch):
        import plugins.context as ctx

        ctx.set_context("sess-5", "ws-from-context", "")
        with patch("plugins.tools.read_workspace_file.read_document_content", return_value={"content": "x", "version_id": "v1"}) as mock_fn:
            from plugins.tools.read_workspace_file import handle

            handle(path="tests/api.go")

        assert mock_fn.call_args.args[0] == "ws-from-context"

    def test_invalid_workspace_id_rejected(self, monkeypatch):
        import plugins.context as ctx

        ctx.set_context("sess-6", "ws-1", "")
        from plugins.tools.read_workspace_file import handle

        result = handle(path="tests/api.go", workspace_id="bad id; DROP TABLE")
        assert result["ok"] is False
        assert "Invalid" in result["error"]

    def test_storage_service_error_returns_ok_false(self, monkeypatch):
        import plugins.context as ctx

        ctx.set_context("sess-7", "ws-1", "")
        from plugins.clients.storage_service_client import StorageServiceError

        with patch(
            "plugins.tools.read_workspace_file.read_document_content",
            side_effect=StorageServiceError("boom", reason_code="request_error"),
        ):
            from plugins.tools.read_workspace_file import handle

            result = handle(path="tests/api.go")

        assert result["ok"] is False
        assert "boom" in result["error"]

    def test_always_reads_with_empty_feature_id(self, monkeypatch):
        """Never leaks a feature scope into the workspace-root read — this
        tool's whole point is reaching documents no feature owns."""
        import plugins.context as ctx

        ctx.set_context("sess-8", "ws-1", "some-feature-in-context")
        with patch("plugins.tools.read_workspace_file.read_document_content", return_value={"content": "x", "version_id": "v1"}) as mock_fn:
            from plugins.tools.read_workspace_file import handle

            handle(path="tests/api.go")

        assert mock_fn.call_args.args[1] == ""


class TestToolsRegistration:
    @staticmethod
    def _get_tools():
        """Return the workflow tool list from the profile setup module."""
        from profiles.workflow.setup import _WORKFLOW_TOOLS
        return _WORKFLOW_TOOLS

    def test_read_workspace_file_in_tools(self):
        names = [t["name"] for t in self._get_tools()]
        assert "read_workspace_file" in names

    def test_read_workspace_file_has_check_fn(self):
        tool = next(t for t in self._get_tools() if t["name"] == "read_workspace_file")
        assert callable(tool.get("check_fn"))
