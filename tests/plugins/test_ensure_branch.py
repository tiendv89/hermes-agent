"""Tests for the vcs_ensure_branch tool.

Covers:
  - handle: happy path — branch ensured, expected fields returned
  - handle: each required field (owner/repo/branch/base_branch) missing returns ok=False
  - handle: 4xx backend error returns ok=False with message
  - handle: 5xx backend error returns ok=False with "request failed" message
  - handle: network/unexpected error returns ok=False
  - handle: fields are stripped before forwarding
  - SCHEMA: required fields, additionalProperties False
  - _TOOLS: vcs_ensure_branch registered with check_fn=check_available
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

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


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("VCS_SERVICE_URL", raising=False)
    monkeypatch.delenv("VCS_SERVICE_TOKEN", raising=False)
    yield


class TestSchema:
    def test_required_fields(self):
        from plugins.tools.ensure_branch import SCHEMA

        assert set(SCHEMA["parameters"]["required"]) == {
            "owner",
            "repo",
            "branch",
            "base_branch",
        }

    def test_additional_properties_false(self):
        from plugins.tools.ensure_branch import SCHEMA

        assert SCHEMA["parameters"].get("additionalProperties") is False


class TestHandleHappyPath:
    def test_successful_call_returns_ok(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        with patch(
            "src.services.vcs_service_client.ensure_branch",
            AsyncMock(return_value={"status": "created"}),
        ):
            from plugins.tools.ensure_branch import handle

            result = handle(
                owner="acme", repo="widgets", branch="feature", base_branch="main"
            )

        assert result["ok"] is True
        assert result["branch"] == "feature"

    def test_fields_stripped_before_forwarding(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        mock_ensure = AsyncMock(return_value={"status": "created"})
        with patch("src.services.vcs_service_client.ensure_branch", mock_ensure):
            from plugins.tools.ensure_branch import handle

            handle(
                owner="  acme  ",
                repo="  widgets  ",
                branch="  feature  ",
                base_branch="  main  ",
            )

        assert mock_ensure.call_args.args == ("acme", "widgets", "feature", "main")


class TestHandleErrors:
    @pytest.mark.parametrize(
        "missing_field", ["owner", "repo", "branch", "base_branch"]
    )
    def test_missing_required_field_returns_error(self, monkeypatch, missing_field):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        from plugins.tools.ensure_branch import handle

        kwargs = {
            "owner": "acme",
            "repo": "widgets",
            "branch": "feature",
            "base_branch": "main",
        }
        kwargs[missing_field] = ""

        result = handle(**kwargs)
        assert result["ok"] is False
        assert missing_field in result["error"]

    def test_4xx_backend_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        from src.services.vcs_service_client import VCSServiceError

        with patch(
            "src.services.vcs_service_client.ensure_branch",
            AsyncMock(
                side_effect=VCSServiceError(
                    "owner, repo, branch, and base_branch are required", status=400
                )
            ),
        ):
            from plugins.tools.ensure_branch import handle

            result = handle(owner="acme", repo="widgets", branch="feature", base_branch="main")

        assert result["ok"] is False
        assert "required" in result["error"]

    def test_5xx_backend_error_returns_request_failed_message(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        from src.services.vcs_service_client import VCSServiceError

        with patch(
            "src.services.vcs_service_client.ensure_branch",
            AsyncMock(side_effect=VCSServiceError("internal server error", status=500)),
        ):
            from plugins.tools.ensure_branch import handle

            result = handle(owner="acme", repo="widgets", branch="feature", base_branch="main")

        assert result["ok"] is False
        assert "request failed" in result["error"]

    def test_network_error_returns_request_failed_message(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        with patch(
            "src.services.vcs_service_client.ensure_branch",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            from plugins.tools.ensure_branch import handle

            result = handle(owner="acme", repo="widgets", branch="feature", base_branch="main")

        assert result["ok"] is False
        assert "request failed" in result["error"]


class TestToolsRegistration:
    @staticmethod
    def _get_tools():
        """Return the workflow tool list from the profile setup module."""
        from profiles.workflow.setup import _WORKFLOW_TOOLS
        return _WORKFLOW_TOOLS

    def test_vcs_ensure_branch_in_tools(self):
        names = [t["name"] for t in self._get_tools()]
        assert "vcs_ensure_branch" in names

    def test_vcs_ensure_branch_has_check_fn(self):
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_ensure_branch")
        assert callable(tool.get("check_fn"))

    def test_vcs_ensure_branch_check_fn_false_without_config(self):
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_ensure_branch")
        assert tool["check_fn"]() is False

    def test_vcs_ensure_branch_check_fn_true_with_config(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_ensure_branch")
        assert tool["check_fn"]() is True

    def test_vcs_ensure_branch_has_handler(self):
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_ensure_branch")
        assert callable(tool.get("handler"))

    def test_vcs_ensure_branch_has_schema(self):
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_ensure_branch")
        assert isinstance(tool.get("schema"), dict)
        assert "parameters" in tool["schema"]
