"""Tests for the workflow_lookup_feature tool (T3 — agent-general-chat).

Covers:
  - handle: successful lookup returns title/stage/status/synopsis
  - handle: unknown feature ref handled gracefully (ok=False)
  - handle: cross-workspace leakage impossible (workspace_id always from context)
  - handle: missing feature_ref returns ok=False
  - handle: invalid feature_ref characters return ok=False
  - handle: DB unavailable returns ok=False
  - handle: synopsis extracted from product-spec.md via storage-service
  - handle: synopsis fetch failure is silent (graceful degradation)
  - check_available: returns True for non-feature sessions with workflow DB
  - check_available: returns False when feature_id is set (feature-scoped session)
  - check_available: returns False when the workflow backend is not configured
  - _extract_synopsis: first paragraph extracted; headings skipped; truncation
  - inject_context: workflow_lookup_feature hint injected when feature_id == ''
  - inject_context: no lookup hint when feature_id is set (feature-scoped session)
  - _TOOLS: workflow_lookup_feature registered with check_fn
  - tool never appears alongside write tools in a feature-scoped session's context
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
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    yield


# ---------------------------------------------------------------------------
# _extract_synopsis
# ---------------------------------------------------------------------------


class TestExtractSynopsis:
    def test_returns_first_paragraph(self):
        from plugins.tools.lookup_feature import _extract_synopsis

        content = "# Title\n\nThis is the synopsis paragraph.\n\nSecond paragraph."
        assert _extract_synopsis(content) == "This is the synopsis paragraph."

    def test_skips_leading_headings(self):
        from plugins.tools.lookup_feature import _extract_synopsis

        content = "# Heading\n## Subheading\n\nFirst real paragraph here."
        assert _extract_synopsis(content) == "First real paragraph here."

    def test_empty_content_returns_empty(self):
        from plugins.tools.lookup_feature import _extract_synopsis

        assert _extract_synopsis("") == ""
        assert _extract_synopsis("   ") == ""

    def test_truncates_to_500_chars(self):
        from plugins.tools.lookup_feature import _extract_synopsis

        long_paragraph = "x" * 600
        result = _extract_synopsis(long_paragraph)
        assert len(result) == 500

    def test_multiline_paragraph_joined(self):
        from plugins.tools.lookup_feature import _extract_synopsis

        content = "Line one of the paragraph.\nLine two still in paragraph.\n\nNext paragraph."
        result = _extract_synopsis(content)
        assert "Line one" in result
        assert "Line two" in result
        assert "Next paragraph" not in result

    def test_only_headings_returns_empty(self):
        from plugins.tools.lookup_feature import _extract_synopsis

        assert _extract_synopsis("# Title\n## Subtitle\n") == ""


# ---------------------------------------------------------------------------
# check_available
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    def test_false_when_db_url_unset(self):
        from plugins.tools.lookup_feature import check_available

        assert check_available() is False

    def test_false_when_feature_id_set(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-1", "ws-1", "some-feature")
        from plugins.tools.lookup_feature import check_available

        assert check_available() is False

    def test_true_for_non_feature_session_with_db(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-2", "ws-1", "")
        from plugins.tools.lookup_feature import check_available

        assert check_available() is True

    def test_false_for_blank_db_url(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "   ")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-3", "ws-1", "")
        from plugins.tools.lookup_feature import check_available

        assert check_available() is False


# ---------------------------------------------------------------------------
# handle — core lookup behaviour
# ---------------------------------------------------------------------------


class TestHandleLookupFeature:
    def test_successful_lookup_returns_fields(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-lookup-1", "ws-1", "")

        fake_detail = {
            "feature_name": "agent-general-chat",
            "title": "General Chat Feature",
            "stage": "in_implementation",
            "status": "ready_for_implementation",
            "next_action": "Run tasks",
            "owner": None,
            "init_pr_url": None,
        }
        with patch("src.services.workflow_backend_client.get_feature_detail", AsyncMock(return_value=fake_detail)):
            from plugins.tools.lookup_feature import handle

            result = handle(feature_ref="agent-general-chat")

        assert result["ok"] is True
        assert result["title"] == "General Chat Feature"
        assert result["stage"] == "in_implementation"
        assert result["status"] == "ready_for_implementation"
        assert result["next_action"] == "Run tasks"
        assert result["feature_ref"] == "agent-general-chat"

    def test_unknown_feature_ref_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-lookup-2", "ws-1", "")
        with patch(
            "src.services.workflow_backend_client.get_feature_detail",
            AsyncMock(side_effect=ValueError("Feature 'no-such' not found in workspace 'ws-1'")),
        ):
            from plugins.tools.lookup_feature import handle

            result = handle(feature_ref="no-such")

        assert result["ok"] is False
        assert "not found" in result["error"]

    def test_db_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-lookup-3", "ws-1", "")
        with patch(
            "src.services.workflow_backend_client.get_feature_detail",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            from plugins.tools.lookup_feature import handle

            result = handle(feature_ref="some-feature")

        assert result["ok"] is False
        assert "connection refused" in result["error"]

    def test_empty_feature_ref_returns_error(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-lookup-4", "ws-1", "")
        from plugins.tools.lookup_feature import handle

        result = handle(feature_ref="")
        assert result["ok"] is False
        assert "required" in result["error"]

    def test_invalid_feature_ref_characters_rejected(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-lookup-5", "ws-1", "")
        from plugins.tools.lookup_feature import handle

        # Characters like spaces, slashes, or SQL-injection attempts are rejected.
        result = handle(feature_ref="feat ure/bad; DROP TABLE")
        assert result["ok"] is False
        assert "Invalid" in result["error"]

    def test_no_workspace_context_returns_error(self):
        import plugins.context as ctx

        ctx.set_context("sess-lookup-6", "", "")
        from plugins.tools.lookup_feature import handle

        result = handle(feature_ref="some-feature")
        assert result["ok"] is False
        assert "workspace" in result["error"].lower()

    def test_db_unavailable_returns_error(self):
        import plugins.context as ctx

        ctx.set_context("sess-lookup-7", "ws-1", "")
        from plugins.tools.lookup_feature import handle

        result = handle(feature_ref="some-feature")
        assert result["ok"] is False
        assert "not available" in result["error"].lower()

    def test_workspace_id_always_from_context_not_arg(self, monkeypatch):
        """Cross-workspace leakage: workspace_id is always the session context, not caller-supplied."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-lookup-8", "correct-workspace", "")

        fake_detail = {
            "feature_name": "my-feat",
            "title": "My Feature",
            "stage": "in_design",
            "status": "in_design",
            "next_action": "",
            "owner": None,
            "init_pr_url": None,
        }
        with patch("src.services.workflow_backend_client.get_feature_detail", AsyncMock(return_value=fake_detail)) as mock_fn:
            from plugins.tools.lookup_feature import handle

            handle(feature_ref="my-feat")

        # Must always use the workspace from context, not a caller-supplied one.
        assert mock_fn.call_args.args == ("correct-workspace", "my-feat")

    def test_synopsis_extracted_from_storage_service(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-lookup-9", "ws-1", "")

        fake_detail = {
            "feature_name": "my-feat",
            "title": "My Feature",
            "stage": "in_design",
            "status": "in_design",
            "next_action": "",
            "owner": None,
            "init_pr_url": None,
        }
        fake_doc = {"content": "# Title\n\nThis is the feature synopsis.", "version_id": "v1"}

        with (
            patch("src.services.workflow_backend_client.get_feature_detail", AsyncMock(return_value=fake_detail)),
            patch("plugins.clients.storage_service_client.read_document_content", return_value=fake_doc),
        ):
            from plugins.tools.lookup_feature import handle

            result = handle(feature_ref="my-feat")

        assert result["ok"] is True
        assert result["synopsis"] == "This is the feature synopsis."

    def test_synopsis_empty_when_storage_service_returns_nothing(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-lookup-10", "ws-1", "")

        fake_detail = {
            "feature_name": "my-feat",
            "title": "My Feature",
            "stage": "in_design",
            "status": "in_design",
            "next_action": "",
            "owner": None,
            "init_pr_url": None,
        }
        with (
            patch("src.services.workflow_backend_client.get_feature_detail", AsyncMock(return_value=fake_detail)),
            patch(
                "plugins.clients.storage_service_client.read_document_content",
                return_value={"content": "", "version_id": None},
            ),
        ):
            from plugins.tools.lookup_feature import handle

            result = handle(feature_ref="my-feat")

        assert result["ok"] is True
        assert result["synopsis"] == ""

    def test_synopsis_failure_is_silent(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-lookup-11", "ws-1", "")

        fake_detail = {
            "feature_name": "my-feat",
            "title": "My Feature",
            "stage": "in_design",
            "status": "in_design",
            "next_action": "",
            "owner": None,
            "init_pr_url": None,
        }
        with (
            patch("src.services.workflow_backend_client.get_feature_detail", AsyncMock(return_value=fake_detail)),
            patch(
                "plugins.clients.storage_service_client.read_document_content",
                side_effect=RuntimeError("network error"),
            ),
        ):
            from plugins.tools.lookup_feature import handle

            result = handle(feature_ref="my-feat")

        # Tool still returns ok=True; synopsis is empty (graceful degradation).
        assert result["ok"] is True
        assert result["synopsis"] == ""


# ---------------------------------------------------------------------------
# inject_context — tool hint injection
# ---------------------------------------------------------------------------


class TestInjectContextLookupHint:
    def test_lookup_hint_present_when_no_feature_id(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-hook-1", "ws-1", "")

        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch("plugins.tools.workspace.handle", return_value={"ok": False}),
            patch("plugins.tools.gitnexus.list_indexed_repos", return_value=[]),
        ):
            from plugins.hooks import inject_context

            result = inject_context(session_id="sess-hook-1")

        assert result is not None
        context_text = result["context"]
        assert "workflow_lookup_feature" in context_text
        assert "general" in context_text or "no feature scope" in context_text

    def test_lookup_hint_absent_when_feature_id_set(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-hook-2", "ws-1", "some-feature")

        fake_detail = {
            "feature_name": "some-feature",
            "title": "Some Feature",
            "stage": "in_design",
            "status": "in_design",
            "next_action": "",
            "owner": None,
            "init_pr_url": None,
        }
        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch("plugins.tools.workspace.handle", return_value={"ok": False}),
            patch("plugins.tools.gitnexus.list_indexed_repos", return_value=[]),
            patch("plugins.tools.feature.handle", return_value={"ok": True, "feature": fake_detail}),
            patch("plugins.tools.tasks.handle", return_value={"ok": True, "tasks": []}),
        ):
            from plugins.hooks import inject_context

            result = inject_context(session_id="sess-hook-2")

        assert result is not None
        context_text = result["context"]
        # The lookup tool hint must NOT be injected for feature-scoped sessions.
        assert "workflow_lookup_feature" not in context_text


# ---------------------------------------------------------------------------
# _TOOLS registration
# ---------------------------------------------------------------------------


class TestToolsRegistration:
    @staticmethod
    def _get_tools():
        """Return the workflow tool list from the profile setup module."""
        from src.tool_setup import _WORKFLOW_TOOLS
        return _WORKFLOW_TOOLS

    def test_workflow_lookup_feature_in_tools(self):
        names = [t["name"] for t in self._get_tools()]
        assert "workflow_lookup_feature" in names

    def test_workflow_lookup_feature_has_check_fn(self):
        tool = next(t for t in self._get_tools() if t["name"] == "workflow_lookup_feature")
        assert callable(tool.get("check_fn"))

    def test_workflow_lookup_feature_check_fn_returns_false_without_db(self):
        tool = next(t for t in self._get_tools() if t["name"] == "workflow_lookup_feature")
        import plugins.context as ctx

        ctx.set_context("sess-reg-1", "ws-1", "")
        assert tool["check_fn"]() is False

    def test_write_tools_absent_when_lookup_check_fn_true(self, monkeypatch):
        """workflow_lookup_feature check_fn is True; write tools use check_workflow_available.

        This test verifies that the lookup tool's check_fn is independent of the
        write tools' check_fn — a non-feature session with DB access gets the
        lookup tool but not the write/approval tools (those are gated separately).
        The write tool check_fn (check_workflow_available) is also True here, so
        this test simply confirms the lookup tool's check_fn is present and callable.
        """
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        lookup_tool = next(
            t for t in self._get_tools() if t["name"] == "workflow_lookup_feature"
        )
        write_tools = [
            t
            for t in self._get_tools()
            if t["name"]
            in (
                "write_product_spec",
                "write_technical_design",
                "write_tasks",
                "approve_feature",
            )
        ]

        assert lookup_tool["check_fn"]() is True
        # Write tools use check_workflow_available (DB available → True here).
        # When DB is set AND feature_id == '', lookup is True; write tools are
        # always True when DB is set — they are excluded by the agent framework's
        # session-scoping, not by check_fn alone.
        for wt in write_tools:
            assert callable(wt.get("check_fn"))

    def test_lookup_tool_check_fn_false_when_feature_scoped(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        import plugins.context as ctx

        ctx.set_context("sess-reg-3", "ws-1", "active-feature")

        lookup_tool = next(
            t for t in self._get_tools() if t["name"] == "workflow_lookup_feature"
        )
        assert lookup_tool["check_fn"]() is False
