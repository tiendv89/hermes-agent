"""Tests for the vcs_create_pr tool.

Covers:
  - handle: happy path — PR created, expected fields returned
  - handle: each required field (owner/repo/title/head/base) missing returns ok=False
  - handle: optional body/draft forwarded to the client
  - handle: 4xx backend error returns ok=False with message
  - handle: 5xx backend error returns ok=False with "request failed" message
  - handle: network/unexpected error returns ok=False
  - handle: fields are stripped before forwarding
  - SCHEMA: required fields, additionalProperties False
  - _TOOLS: vcs_create_pr registered with check_fn=check_available
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Module helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# SCHEMA validation
# ---------------------------------------------------------------------------


class TestSchema:
    def test_required_fields(self):
        from plugins.tools.create_pr import SCHEMA

        assert set(SCHEMA["parameters"]["required"]) == {
            "owner",
            "repo",
            "title",
            "head",
            "base",
        }

    def test_optional_body_and_draft_defined(self):
        from plugins.tools.create_pr import SCHEMA

        props = SCHEMA["parameters"]["properties"]
        assert "body" in props and "body" not in SCHEMA["parameters"]["required"]
        assert "draft" in props and "draft" not in SCHEMA["parameters"]["required"]

    def test_additional_properties_false(self):
        from plugins.tools.create_pr import SCHEMA

        assert SCHEMA["parameters"].get("additionalProperties") is False


# ---------------------------------------------------------------------------
# handle — happy path
# ---------------------------------------------------------------------------


class TestHandleHappyPath:
    def test_successful_creation_returns_expected_fields(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        fake_response = {
            "number": 42,
            "html_url": "https://github.com/acme/widgets/pull/42",
            "state": "open",
            "head_ref": "feature/retry-logic",
            "base_ref": "main",
        }
        with patch(
            "src.services.vcs_service_client.create_pr",
            AsyncMock(return_value=fake_response),
        ):
            from plugins.tools.create_pr import handle

            result = handle(
                owner="acme",
                repo="widgets",
                title="Add retry logic",
                head="feature/retry-logic",
                base="main",
            )

        assert result["ok"] is True
        assert result["number"] == 42
        assert result["html_url"] == "https://github.com/acme/widgets/pull/42"
        assert result["state"] == "open"
        assert result["head_ref"] == "feature/retry-logic"
        assert result["base_ref"] == "main"

    def test_optional_body_and_draft_forwarded(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        fake_response = {"number": 1}
        mock_create = AsyncMock(return_value=fake_response)
        with patch("src.services.vcs_service_client.create_pr", mock_create):
            from plugins.tools.create_pr import handle

            handle(
                owner="acme",
                repo="widgets",
                title="Title",
                head="feature",
                base="main",
                body="Optional description",
                draft=True,
            )

        assert mock_create.call_args.kwargs["body"] == "Optional description"
        assert mock_create.call_args.kwargs["draft"] is True

    def test_fields_stripped_before_forwarding(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        fake_response = {"number": 1}
        mock_create = AsyncMock(return_value=fake_response)
        with patch("src.services.vcs_service_client.create_pr", mock_create):
            from plugins.tools.create_pr import handle

            handle(
                owner="  acme  ",
                repo="  widgets  ",
                title="  Title  ",
                head="  feature  ",
                base="  main  ",
            )

        assert mock_create.call_args.args == ("acme", "widgets", "Title", "feature", "main")


# ---------------------------------------------------------------------------
# handle — validation / error paths
# ---------------------------------------------------------------------------


class TestHandleErrors:
    @pytest.mark.parametrize(
        "missing_field",
        ["owner", "repo", "title", "head", "base"],
    )
    def test_missing_required_field_returns_error(self, monkeypatch, missing_field):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        from plugins.tools.create_pr import handle

        kwargs = {
            "owner": "acme",
            "repo": "widgets",
            "title": "Title",
            "head": "feature",
            "base": "main",
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
            "src.services.vcs_service_client.create_pr",
            AsyncMock(
                side_effect=VCSServiceError(
                    "vcs-service returned HTTP 422: base branch not found", status=422
                )
            ),
        ):
            from plugins.tools.create_pr import handle

            result = handle(
                owner="acme", repo="widgets", title="Title", head="feature", base="ghost"
            )

        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_5xx_backend_error_returns_request_failed_message(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        from src.services.vcs_service_client import VCSServiceError

        with patch(
            "src.services.vcs_service_client.create_pr",
            AsyncMock(
                side_effect=VCSServiceError("internal server error", status=500)
            ),
        ):
            from plugins.tools.create_pr import handle

            result = handle(
                owner="acme", repo="widgets", title="Title", head="feature", base="main"
            )

        assert result["ok"] is False
        assert "request failed" in result["error"]

    def test_network_error_returns_request_failed_message(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")

        with patch(
            "src.services.vcs_service_client.create_pr",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            from plugins.tools.create_pr import handle

            result = handle(
                owner="acme", repo="widgets", title="Title", head="feature", base="main"
            )

        assert result["ok"] is False
        assert "request failed" in result["error"]


# ---------------------------------------------------------------------------
# _TOOLS registration
# ---------------------------------------------------------------------------


class TestToolsRegistration:
    def test_vcs_create_pr_in_tools(self):
        plugins = _load_plugins_init()
        names = [t["name"] for t in plugins._TOOLS]
        assert "vcs_create_pr" in names

    def test_vcs_create_pr_has_check_fn(self):
        plugins = _load_plugins_init()
        tool = next(t for t in plugins._TOOLS if t["name"] == "vcs_create_pr")
        assert callable(tool.get("check_fn"))

    def test_vcs_create_pr_check_fn_false_without_config(self):
        plugins = _load_plugins_init()
        tool = next(t for t in plugins._TOOLS if t["name"] == "vcs_create_pr")
        assert tool["check_fn"]() is False

    def test_vcs_create_pr_check_fn_true_with_config(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        plugins = _load_plugins_init()
        tool = next(t for t in plugins._TOOLS if t["name"] == "vcs_create_pr")
        assert tool["check_fn"]() is True

    def test_vcs_create_pr_has_handler(self):
        plugins = _load_plugins_init()
        tool = next(t for t in plugins._TOOLS if t["name"] == "vcs_create_pr")
        assert callable(tool.get("handler"))

    def test_vcs_create_pr_has_schema(self):
        plugins = _load_plugins_init()
        tool = next(t for t in plugins._TOOLS if t["name"] == "vcs_create_pr")
        assert isinstance(tool.get("schema"), dict)
        assert "parameters" in tool["schema"]
