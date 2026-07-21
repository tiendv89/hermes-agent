"""Tests for the vcs_commit_files tool.

Covers:
  - handle: happy path — files committed, expected fields returned
  - handle: each required field (owner/repo/branch/message/files) missing returns ok=False
  - handle: empty files dict returns ok=False
  - handle: optional base_branch forwarded when present, omitted when empty
  - handle: 4xx backend error returns ok=False with message
  - handle: 5xx backend error returns ok=False with "request failed" message
  - handle: network/unexpected error returns ok=False
  - handle: string fields are stripped before forwarding (files values are not)
  - SCHEMA: required fields, additionalProperties False
  - _TOOLS: vcs_commit_files registered with check_fn=check_available
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
        from plugins.tools.commit_files import SCHEMA

        assert set(SCHEMA["parameters"]["required"]) == {
            "owner",
            "repo",
            "branch",
            "message",
            "files",
        }

    def test_optional_base_branch_defined(self):
        from plugins.tools.commit_files import SCHEMA

        props = SCHEMA["parameters"]["properties"]
        assert "base_branch" in props
        assert "base_branch" not in SCHEMA["parameters"]["required"]

    def test_additional_properties_false(self):
        from plugins.tools.commit_files import SCHEMA

        assert SCHEMA["parameters"].get("additionalProperties") is False


class TestHandleHappyPath:
    def test_successful_call_returns_ok(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        with patch(
            "src.services.vcs_service_client.commit_files",
            AsyncMock(return_value={"status": "committed"}),
        ):
            from plugins.tools.commit_files import handle

            result = handle(
                owner="acme",
                repo="widgets",
                branch="feature",
                message="test commit",
                files={"README.md": "test"},
            )

        assert result["ok"] is True
        assert result["branch"] == "feature"
        assert result["files_committed"] == ["README.md"]

    def test_base_branch_forwarded_when_provided(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        mock_commit = AsyncMock(return_value={"status": "committed"})
        with patch("src.services.vcs_service_client.commit_files", mock_commit):
            from plugins.tools.commit_files import handle

            handle(
                owner="acme",
                repo="widgets",
                branch="feature",
                message="test commit",
                files={"README.md": "test"},
                base_branch="main",
            )

        assert mock_commit.call_args.kwargs["base_branch"] == "main"

    def test_base_branch_omitted_when_empty(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        mock_commit = AsyncMock(return_value={"status": "committed"})
        with patch("src.services.vcs_service_client.commit_files", mock_commit):
            from plugins.tools.commit_files import handle

            handle(
                owner="acme",
                repo="widgets",
                branch="feature",
                message="test commit",
                files={"README.md": "test"},
            )

        assert mock_commit.call_args.kwargs["base_branch"] == ""

    def test_files_values_not_stripped(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        mock_commit = AsyncMock(return_value={"status": "committed"})
        with patch("src.services.vcs_service_client.commit_files", mock_commit):
            from plugins.tools.commit_files import handle

            handle(
                owner="  acme  ",
                repo="  widgets  ",
                branch="  feature  ",
                message="  test commit  ",
                files={"README.md": "  leading/trailing whitespace preserved  "},
            )

        assert mock_commit.call_args.args == (
            "acme",
            "widgets",
            "feature",
            "test commit",
            {"README.md": "  leading/trailing whitespace preserved  "},
        )


class TestHandleErrors:
    @pytest.mark.parametrize(
        "missing_field", ["owner", "repo", "branch", "message"]
    )
    def test_missing_required_string_field_returns_error(self, monkeypatch, missing_field):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        from plugins.tools.commit_files import handle

        kwargs = {
            "owner": "acme",
            "repo": "widgets",
            "branch": "feature",
            "message": "test commit",
            "files": {"README.md": "test"},
        }
        kwargs[missing_field] = ""

        result = handle(**kwargs)
        assert result["ok"] is False
        assert missing_field in result["error"]

    def test_empty_files_returns_error(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        from plugins.tools.commit_files import handle

        result = handle(
            owner="acme", repo="widgets", branch="feature", message="test commit", files={}
        )
        assert result["ok"] is False
        assert "files" in result["error"]

    def test_4xx_backend_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        from src.services.vcs_service_client import VCSServiceError

        with patch(
            "src.services.vcs_service_client.commit_files",
            AsyncMock(
                side_effect=VCSServiceError(
                    "owner, repo, branch, message, and files are required", status=400
                )
            ),
        ):
            from plugins.tools.commit_files import handle

            result = handle(
                owner="acme",
                repo="widgets",
                branch="feature",
                message="test commit",
                files={"README.md": "test"},
            )

        assert result["ok"] is False
        assert "required" in result["error"]

    def test_5xx_backend_error_returns_request_failed_message(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        from src.services.vcs_service_client import VCSServiceError

        with patch(
            "src.services.vcs_service_client.commit_files",
            AsyncMock(side_effect=VCSServiceError("internal server error", status=500)),
        ):
            from plugins.tools.commit_files import handle

            result = handle(
                owner="acme",
                repo="widgets",
                branch="feature",
                message="test commit",
                files={"README.md": "test"},
            )

        assert result["ok"] is False
        assert "request failed" in result["error"]

    def test_network_error_returns_request_failed_message(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        with patch(
            "src.services.vcs_service_client.commit_files",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            from plugins.tools.commit_files import handle

            result = handle(
                owner="acme",
                repo="widgets",
                branch="feature",
                message="test commit",
                files={"README.md": "test"},
            )

        assert result["ok"] is False
        assert "request failed" in result["error"]


class TestToolsRegistration:
    @staticmethod
    def _get_tools():
        """Return the workflow tool list from the profile setup module."""
        from profiles.workflow.setup import _WORKFLOW_TOOLS
        return _WORKFLOW_TOOLS

    def test_vcs_commit_files_in_tools(self):
        names = [t["name"] for t in self._get_tools()]
        assert "vcs_commit_files" in names

    def test_vcs_commit_files_has_check_fn(self):
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_commit_files")
        assert callable(tool.get("check_fn"))

    def test_vcs_commit_files_check_fn_false_without_config(self):
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_commit_files")
        assert tool["check_fn"]() is False

    def test_vcs_commit_files_check_fn_true_with_config(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_commit_files")
        assert tool["check_fn"]() is True

    def test_vcs_commit_files_has_handler(self):
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_commit_files")
        assert callable(tool.get("handler"))

    def test_vcs_commit_files_has_schema(self):
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_commit_files")
        assert isinstance(tool.get("schema"), dict)
        assert "parameters" in tool["schema"]
