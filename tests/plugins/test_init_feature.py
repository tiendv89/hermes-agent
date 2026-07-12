"""Tests for the workflow_init_feature tool (m3-agent-init-feature-command / T1).

Covers:
  - handle: happy path — feature created, owner always "go", feature_id and init_pr_url returned
  - handle: owner is hardcoded "go" — not in schema, never overridable
  - handle: empty name returns ok=False
  - handle: blank-only name returns ok=False
  - handle: missing workspace context returns ok=False
  - handle: 4xx backend error (validation / duplicate name) returns ok=False with message
  - handle: 5xx backend error returns ok=False with "request failed" message
  - handle: network/unexpected error returns ok=False
  - handle: optional description forwarded to backend
  - handle: optional start_stage forwarded when present, omitted when empty
  - handle: name is stripped before forwarding
  - SCHEMA: no "owner" property defined
  - SCHEMA: "name" is in required
  - SCHEMA: additionalProperties is False
  - _TOOLS: workflow_init_feature registered with check_fn=check_workflow_available
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
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
    yield


# ---------------------------------------------------------------------------
# SCHEMA validation
# ---------------------------------------------------------------------------


class TestSchema:
    def test_name_is_required(self):
        from plugins.tools.init_feature import SCHEMA

        assert "name" in SCHEMA["parameters"]["required"]

    def test_no_owner_property(self):
        from plugins.tools.init_feature import SCHEMA

        assert "owner" not in SCHEMA["parameters"]["properties"]

    def test_additional_properties_false(self):
        from plugins.tools.init_feature import SCHEMA

        assert SCHEMA["parameters"].get("additionalProperties") is False

    def test_optional_description_defined(self):
        from plugins.tools.init_feature import SCHEMA

        assert "description" in SCHEMA["parameters"]["properties"]
        assert "description" not in SCHEMA["parameters"]["required"]

    def test_optional_start_stage_defined(self):
        from plugins.tools.init_feature import SCHEMA

        assert "start_stage" in SCHEMA["parameters"]["properties"]
        assert "start_stage" not in SCHEMA["parameters"]["required"]


# ---------------------------------------------------------------------------
# handle — happy path
# ---------------------------------------------------------------------------


class TestHandleHappyPath:
    def test_successful_creation_returns_expected_fields(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-1", "ws-abc", "")

        fake_response = {
            "feature_id": "feat-uuid-123",
            "init_pr_url": "https://github.com/org/repo/pull/42",
            "owner": "go",
        }
        with patch(
            "src.services.workflow_backend_client.create_feature",
            AsyncMock(return_value=fake_response),
        ):
            from plugins.tools.init_feature import handle

            result = handle(name="My New Feature")

        assert result["ok"] is True
        assert result["feature_id"] == "feat-uuid-123"
        assert result["init_pr_url"] == "https://github.com/org/repo/pull/42"
        assert result["owner"] == "go"

    def test_owner_always_go_in_response(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-owner", "ws-abc", "")

        fake_response = {"feature_id": "feat-42", "init_pr_url": None, "owner": "ts"}
        with patch(
            "src.services.workflow_backend_client.create_feature",
            AsyncMock(return_value=fake_response),
        ):
            from plugins.tools.init_feature import handle

            result = handle(name="My Feature")

        # Response owner is always "go" regardless of what backend returns.
        assert result["owner"] == "go"

    def test_description_forwarded_to_backend(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-desc", "ws-abc", "")

        fake_response = {"feature_id": "feat-99", "init_pr_url": None}
        mock_create = AsyncMock(return_value=fake_response)
        with patch("src.services.workflow_backend_client.create_feature", mock_create):
            from plugins.tools.init_feature import handle

            handle(name="Feature X", description="A detailed description")

        assert mock_create.call_args.kwargs["description"] == "A detailed description"

    def test_start_stage_forwarded_when_provided(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-stage", "ws-abc", "")

        fake_response = {"feature_id": "feat-99", "init_pr_url": None}
        mock_create = AsyncMock(return_value=fake_response)
        with patch("src.services.workflow_backend_client.create_feature", mock_create):
            from plugins.tools.init_feature import handle

            handle(name="Feature Y", start_stage="in_design")

        assert mock_create.call_args.kwargs["start_stage"] == "in_design"

    def test_start_stage_omitted_when_empty(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-stage-empty", "ws-abc", "")

        fake_response = {"feature_id": "feat-99", "init_pr_url": None}
        mock_create = AsyncMock(return_value=fake_response)
        with patch("src.services.workflow_backend_client.create_feature", mock_create):
            from plugins.tools.init_feature import handle

            handle(name="Feature Z", start_stage="")

        assert mock_create.call_args.kwargs["start_stage"] is None

    def test_name_stripped_before_forwarding(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-strip", "ws-abc", "")

        fake_response = {"feature_id": "feat-99", "init_pr_url": None}
        mock_create = AsyncMock(return_value=fake_response)
        with patch("src.services.workflow_backend_client.create_feature", mock_create):
            from plugins.tools.init_feature import handle

            handle(name="  My Feature  ")

        # Positional first arg to create_feature is the stripped name.
        assert mock_create.call_args.args[1] == "My Feature"

    def test_workspace_id_always_from_context(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-ws", "correct-ws", "")

        fake_response = {"feature_id": "feat-x", "init_pr_url": None}
        mock_create = AsyncMock(return_value=fake_response)
        with patch("src.services.workflow_backend_client.create_feature", mock_create):
            from plugins.tools.init_feature import handle

            handle(name="Feature")

        # workspace_id is the first positional arg to create_feature.
        assert mock_create.call_args.args[0] == "correct-ws"

    def test_id_field_fallback(self, monkeypatch):
        """feature_id falls back to response 'id' when 'feature_id' is absent."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-idfb", "ws-abc", "")

        fake_response = {"id": "feat-uuid-fallback", "init_pr_url": None}
        with patch(
            "src.services.workflow_backend_client.create_feature",
            AsyncMock(return_value=fake_response),
        ):
            from plugins.tools.init_feature import handle

            result = handle(name="Feature FB")

        assert result["feature_id"] == "feat-uuid-fallback"


# ---------------------------------------------------------------------------
# handle — validation / error paths
# ---------------------------------------------------------------------------


class TestHandleErrors:
    def test_empty_name_returns_error(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-err-name", "ws-abc", "")
        from plugins.tools.init_feature import handle

        result = handle(name="")
        assert result["ok"] is False
        assert "required" in result["error"].lower()

    def test_blank_name_returns_error(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-err-blank", "ws-abc", "")
        from plugins.tools.init_feature import handle

        result = handle(name="   ")
        assert result["ok"] is False
        assert "required" in result["error"].lower()

    def test_missing_workspace_context_returns_error(self):
        import plugins.context as ctx

        ctx.set_context("sess-err-ws", "", "")
        from plugins.tools.init_feature import handle

        result = handle(name="My Feature")
        assert result["ok"] is False
        assert "workspace" in result["error"].lower()

    def test_4xx_backend_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-err-4xx", "ws-abc", "")

        from src.services.workflow_backend_client import WorkflowBackendError

        with patch(
            "src.services.workflow_backend_client.create_feature",
            AsyncMock(
                side_effect=WorkflowBackendError(
                    "name already exists",
                    reason_code="duplicate_name",
                    status=409,
                )
            ),
        ):
            from plugins.tools.init_feature import handle

            result = handle(name="Existing Feature")

        assert result["ok"] is False
        assert "already exists" in result["error"] or result["error"]

    def test_422_validation_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-err-422", "ws-abc", "")

        from src.services.workflow_backend_client import WorkflowBackendError

        with patch(
            "src.services.workflow_backend_client.create_feature",
            AsyncMock(
                side_effect=WorkflowBackendError(
                    "name is required",
                    reason_code="validation_error",
                    status=422,
                )
            ),
        ):
            from plugins.tools.init_feature import handle

            result = handle(name="X")

        assert result["ok"] is False

    def test_5xx_backend_error_returns_request_failed_message(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-err-5xx", "ws-abc", "")

        from src.services.workflow_backend_client import WorkflowBackendError

        with patch(
            "src.services.workflow_backend_client.create_feature",
            AsyncMock(
                side_effect=WorkflowBackendError(
                    "internal server error",
                    reason_code="internal_error",
                    status=500,
                )
            ),
        ):
            from plugins.tools.init_feature import handle

            result = handle(name="My Feature")

        assert result["ok"] is False
        assert "request failed" in result["error"]

    def test_network_error_returns_request_failed_message(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-err-net", "ws-abc", "")

        with patch(
            "src.services.workflow_backend_client.create_feature",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            from plugins.tools.init_feature import handle

            result = handle(name="My Feature")

        assert result["ok"] is False
        assert "request failed" in result["error"]


# ---------------------------------------------------------------------------
# _TOOLS registration
# ---------------------------------------------------------------------------


class TestToolsRegistration:
    def test_workflow_init_feature_in_tools(self):
        plugins = _load_plugins_init()
        names = [t["name"] for t in plugins._TOOLS]
        assert "workflow_init_feature" in names

    def test_workflow_init_feature_has_check_fn(self):
        plugins = _load_plugins_init()
        tool = next(t for t in plugins._TOOLS if t["name"] == "workflow_init_feature")
        assert callable(tool.get("check_fn"))

    def test_workflow_init_feature_check_fn_false_without_db(self):
        plugins = _load_plugins_init()
        tool = next(t for t in plugins._TOOLS if t["name"] == "workflow_init_feature")
        assert tool["check_fn"]() is False

    def test_workflow_init_feature_check_fn_true_with_db(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        plugins = _load_plugins_init()
        tool = next(t for t in plugins._TOOLS if t["name"] == "workflow_init_feature")
        assert tool["check_fn"]() is True

    def test_workflow_init_feature_has_handler(self):
        plugins = _load_plugins_init()
        tool = next(t for t in plugins._TOOLS if t["name"] == "workflow_init_feature")
        assert callable(tool.get("handler"))

    def test_workflow_init_feature_has_schema(self):
        plugins = _load_plugins_init()
        tool = next(t for t in plugins._TOOLS if t["name"] == "workflow_init_feature")
        assert isinstance(tool.get("schema"), dict)
        assert "parameters" in tool["schema"]
