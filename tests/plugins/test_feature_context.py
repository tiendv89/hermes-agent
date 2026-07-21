"""
Tests for plugins/feature_context.py — get_feature_context() and helpers.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    """Remove plugins modules between tests to avoid cross-test pollution."""
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]


def _set_thread_context(
    workspace_id: str = "ws-1",
    feature_id: str = "feat-1",
    user_id: str = "user-1",
    org_id: str = "org-1",
) -> None:
    """Set thread-local context for feature_context.py to read."""
    import plugins.context as ctx

    ctx.set_context("sess-test", workspace_id, feature_id, user_id, org_id)


# ---------------------------------------------------------------------------
# _Missing
# ---------------------------------------------------------------------------


class TestMissing:
    def test_missing_stores_reason(self):
        from plugins.feature_context import _Missing

        m = _Missing("test reason")
        assert m.reason == "test reason"

    def test_missing_is_not_exception(self):
        from plugins.feature_context import _Missing

        m = _Missing("test")
        assert not isinstance(m, BaseException)


# ---------------------------------------------------------------------------
# _summarize_markdown
# ---------------------------------------------------------------------------


class TestSummarizeMarkdown:
    def test_short_content_returned_verbatim(self):
        from plugins.feature_context import _summarize_markdown

        content = "line1\nline2\nline3"
        result = _summarize_markdown(content, max_lines=5)
        assert result == content

    def test_exact_boundary_returned_verbatim(self):
        from plugins.feature_context import _summarize_markdown

        content = "\n".join(f"line{i}" for i in range(20))
        result = _summarize_markdown(content, max_lines=20)
        assert result == content
        assert "+more" not in result

    def test_long_content_truncated(self):
        from plugins.feature_context import _summarize_markdown

        lines = [f"line{i}" for i in range(50)]
        content = "\n".join(lines)
        result = _summarize_markdown(content, max_lines=20)
        assert "+30 more lines" in result
        assert "use read_file for full content" in result
        # First 20 lines preserved
        for i in range(20):
            assert f"line{i}" in result
        # Line 20+ not present
        assert "line20" not in result
        assert "line49" not in result

    def test_default_max_lines_is_20(self):
        from plugins.feature_context import _summarize_markdown

        lines = [f"line{i}" for i in range(30)]
        content = "\n".join(lines)
        result = _summarize_markdown(content)
        assert "+10 more lines" in result


# ---------------------------------------------------------------------------
# _format_context_block
# ---------------------------------------------------------------------------


class TestFormatContextBlock:
    def _make_feature_state(self, **overrides):
        return {
            "stage": "in_implementation",
            "status": "active",
            "owner": "alice",
            **overrides,
        }

    def _make_tasks(self, *task_specs):
        return [
            {
                "task_name": spec.get("task_name", "T1"),
                "title": spec.get("title", "Task 1"),
                "status": spec.get("status", "todo"),
                "blocked_reason": spec.get("blocked_reason"),
                "blocked_suggestion": spec.get("blocked_suggestion"),
                "depends_on": spec.get("depends_on", []),
                "pr": spec.get("pr"),
            }
            for spec in task_specs
        ]

    def test_lifecycle_section_with_state(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=self._make_feature_state(),
            tasks=_Missing("unavailable"),
            product_spec=_Missing("no spec"),
            technical_design=_Missing("no design"),
        )
        assert "## Feature Context (auto-loaded)" in block
        assert "### Lifecycle" in block
        assert "- Stage: in_implementation" in block
        assert "- Status: active" in block
        assert "- Owner: alice" in block

    def test_lifecycle_section_missing(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("workflow-backend unavailable"),
            tasks=_Missing("unavailable"),
            product_spec=_Missing("no spec"),
            technical_design=_Missing("no design"),
        )
        assert "(unavailable: workflow-backend unavailable)" in block

    def test_lifecycle_without_owner(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state={"stage": "backlog", "status": "draft"},
            tasks=_Missing("unavailable"),
            product_spec=_Missing("no spec"),
            technical_design=_Missing("no design"),
        )
        assert "Owner" not in block

    def test_tasks_section_with_tasks(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("unavailable"),
            tasks=self._make_tasks(
                {"task_name": "T1", "title": "Setup", "status": "done"},
                {"task_name": "T2", "title": "Build API", "status": "in_progress",
                 "depends_on": ["T1"], "pr": {"url": "https://github.com/pr/1"}},
            ),
            product_spec=_Missing("no spec"),
            technical_design=_Missing("no design"),
        )
        assert "### Tasks (live status)" in block
        assert "| Task | Title | Status | Depends On | PR |" in block
        assert "T1" in block
        assert "Setup" in block
        assert "done" in block
        assert "T2" in block
        assert "Build API" in block
        assert "in_progress" in block
        assert "T1" in block  # depends_on
        assert "https://github.com/pr/1" in block

    def test_tasks_section_empty(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("unavailable"),
            tasks=[],
            product_spec=_Missing("no spec"),
            technical_design=_Missing("no design"),
        )
        assert "No tasks created yet." in block

    def test_tasks_section_missing(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("unavailable"),
            tasks=_Missing("workflow-backend unavailable"),
            product_spec=_Missing("no spec"),
            technical_design=_Missing("no design"),
        )
        assert "(unavailable: workflow-backend unavailable)" in block

    def test_blocked_task_shows_reason_and_suggestion(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("unavailable"),
            tasks=self._make_tasks(
                {
                    "task_name": "T1",
                    "title": "Blocked task",
                    "status": "blocked",
                    "blocked_reason": "waiting for dependency",
                    "blocked_suggestion": "Complete T0 first",
                }
            ),
            product_spec=_Missing("no spec"),
            technical_design=_Missing("no design"),
        )
        assert "⛔ **Blocked:** waiting for dependency" in block
        assert "→ Complete T0 first" in block

    def test_product_spec_section(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("unavailable"),
            tasks=_Missing("unavailable"),
            product_spec="# Product Spec\n\nThis is a test spec.",
            technical_design=_Missing("no design"),
        )
        assert "### Product Spec" in block
        assert "# Product Spec" in block
        assert "This is a test spec." in block

    def test_product_spec_missing(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("unavailable"),
            tasks=_Missing("unavailable"),
            product_spec=_Missing("no product_spec yet"),
            technical_design=_Missing("no design"),
        )
        assert "(unavailable: no product_spec yet)" in block

    def test_technical_design_section(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("unavailable"),
            tasks=_Missing("unavailable"),
            product_spec=_Missing("no spec"),
            technical_design="# Tech Design\n\nArchitecture details.",
        )
        assert "### Technical Design" in block
        assert "# Tech Design" in block

    def test_footer_present(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("a"),
            tasks=_Missing("b"),
            product_spec=_Missing("c"),
            technical_design=_Missing("d"),
        )
        assert "Use `get_feature_state`, `get_tasks`, and `read_file`" in block

    def test_tasks_without_pr_field(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("unavailable"),
            tasks=[{"task_name": "T1", "title": "No PR", "status": "todo", "depends_on": []}],
            product_spec=_Missing("no spec"),
            technical_design=_Missing("no design"),
        )
        # PR column should show "—"
        assert "| T1 | No PR | todo | — | — |" in block

    def test_tasks_with_unknown_fields_fallback(self):
        from plugins.feature_context import _Missing, _format_context_block

        block = _format_context_block(
            feature_id="feat-1",
            state=_Missing("a"),
            tasks=[{}],  # completely empty task dict
            product_spec=_Missing("b"),
            technical_design=_Missing("c"),
        )
        assert "| ? | ? | ? | — | — |" in block


# ---------------------------------------------------------------------------
# get_feature_context — integration tests with mocked dependencies
# ---------------------------------------------------------------------------


class TestGetFeatureContext:
    def _make_state(self, **overrides):
        return {
            "stage": "in_implementation",
            "status": "active",
            "owner": "alice",
            **overrides,
        }

    def _make_tasks(self):
        return [
            {
                "task_name": "T1",
                "title": "Setup",
                "status": "done",
                "blocked_reason": None,
                "blocked_suggestion": None,
                "depends_on": [],
                "pr": None,
                "execution": None,
            },
            {
                "task_name": "T2",
                "title": "Build",
                "status": "in_progress",
                "blocked_reason": None,
                "blocked_suggestion": None,
                "depends_on": ["T1"],
                "pr": {"url": "https://github.com/pr/2"},
                "execution": None,
            },
        ]

    def test_happy_path_all_sections_present(self):
        """All four fetches succeed — block contains all sections."""
        _set_thread_context()

        with (
            patch(
                "plugins.feature_context.check_workflow_available",
                return_value=True,
            ),
            patch(
                "plugins.feature_context.run_async",
                side_effect=[self._make_state(), self._make_tasks()],
            ),
            patch(
                "plugins.clients.storage_service_client.read_document_content",
                side_effect=[
                    {"content": "# Spec\n\nSpec content.", "version_id": "v1"},
                    {"content": "# Design\n\nDesign content.", "version_id": "v2"},
                ],
            ),
        ):
            from plugins.feature_context import get_feature_context

            block = get_feature_context()

        assert "## Feature Context (auto-loaded)" in block
        assert "### Lifecycle" in block
        assert "- Stage: in_implementation" in block
        assert "### Tasks (live status)" in block
        assert "T1" in block
        assert "T2" in block
        assert "### Product Spec" in block
        assert "# Spec" in block
        assert "### Technical Design" in block
        assert "# Design" in block

    def test_no_feature_id_returns_empty(self):
        """When feature_id is not set, returns empty string."""
        _set_thread_context(feature_id="")

        with (
            patch("plugins.feature_context.check_workflow_available", return_value=True),
        ):
            from plugins.feature_context import get_feature_context

            block = get_feature_context()

        assert block == ""

    def test_workflow_backend_unavailable(self):
        """When workflow-backend is down, state and tasks show unavailable."""
        _set_thread_context()

        with (
            patch(
                "plugins.feature_context.check_workflow_available",
                return_value=False,
            ),
            patch(
                "plugins.clients.storage_service_client.read_document_content",
                side_effect=[
                    {"content": "# Spec\n\nSpec content.", "version_id": "v1"},
                    {"content": "# Design\n\nDesign content.", "version_id": "v2"},
                ],
            ),
        ):
            from plugins.feature_context import get_feature_context

            block = get_feature_context()

        assert "(unavailable: workflow-backend unavailable)" in block
        # Spec and design still loaded
        assert "### Product Spec" in block
        assert "# Spec" in block
        assert "### Technical Design" in block
        assert "# Design" in block

    def test_no_spec_yet(self):
        """When storage-service returns empty content, shows 'no spec yet'."""
        _set_thread_context()

        with (
            patch(
                "plugins.feature_context.check_workflow_available",
                return_value=True,
            ),
            patch(
                "plugins.feature_context.run_async",
                side_effect=[self._make_state(), self._make_tasks()],
            ),
            patch(
                "plugins.clients.storage_service_client.read_document_content",
                side_effect=[
                    {"content": "", "version_id": None},
                    {"content": "# Design", "version_id": "v2"},
                ],
            ),
        ):
            from plugins.feature_context import get_feature_context

            block = get_feature_context()

        assert "(unavailable: no product_spec yet)" in block
        # Design still loaded
        assert "### Technical Design" in block
        assert "# Design" in block

    def test_no_tasks_created(self):
        """When feature has no tasks, shows 'No tasks created yet.'."""
        _set_thread_context()

        with (
            patch(
                "plugins.feature_context.check_workflow_available",
                return_value=True,
            ),
            patch(
                "plugins.feature_context.run_async",
                side_effect=[self._make_state(), []],
            ),
            patch(
                "plugins.clients.storage_service_client.read_document_content",
                side_effect=[
                    {"content": "# Spec", "version_id": "v1"},
                    {"content": "# Design", "version_id": "v2"},
                ],
            ),
        ):
            from plugins.feature_context import get_feature_context

            block = get_feature_context()

        assert "No tasks created yet." in block

    def test_single_fetch_exception_non_blocking(self):
        """If one fetch raises, others still complete."""
        _set_thread_context()

        with (
            patch(
                "plugins.feature_context.check_workflow_available",
                return_value=True,
            ),
            patch(
                "plugins.feature_context.run_async",
                side_effect=[RuntimeError("boom"), self._make_tasks()],
            ),
            patch(
                "plugins.clients.storage_service_client.read_document_content",
                side_effect=[
                    {"content": "# Spec", "version_id": "v1"},
                    {"content": "# Design", "version_id": "v2"},
                ],
            ),
        ):
            from plugins.feature_context import get_feature_context

            block = get_feature_context()

        # State fetch failed
        assert "get_feature_state failed" in block
        # Tasks still loaded
        assert "T1" in block
        # Spec and design still loaded
        assert "### Product Spec" in block
        assert "### Technical Design" in block

    def test_document_read_exception_non_blocking(self):
        """If document read raises, it's captured as _Missing."""
        _set_thread_context()

        with (
            patch(
                "plugins.feature_context.check_workflow_available",
                return_value=True,
            ),
            patch(
                "plugins.feature_context.run_async",
                side_effect=[self._make_state(), self._make_tasks()],
            ),
            patch(
                "plugins.clients.storage_service_client.read_document_content",
                side_effect=[
                    Exception("storage down"),
                    {"content": "# Design", "version_id": "v2"},
                ],
            ),
        ):
            from plugins.feature_context import get_feature_context

            block = get_feature_context()

        assert "read product_spec.md failed" in block
        assert "### Technical Design" in block


# ---------------------------------------------------------------------------
# inject_context — feature block integration
# ---------------------------------------------------------------------------


class TestInjectContextFeatureBlock:
    def _call_inject(self, workspace_id="ws-1", feature_id="feat-1") -> str:
        from plugins.context import set_context
        from plugins.hooks import inject_context

        session_id = "sess-test"
        set_context(session_id, workspace_id, feature_id)
        result = inject_context(session_id=session_id)
        return result["context"] if result else ""

    def _make_workspace_result(self):
        return {"ok": True, "workspace": {"repos": [{"id": "hermes-agent"}]}}

    def _make_feature_result(self):
        return {"ok": True, "feature": {"stage": "in_implementation"}}

    def test_feature_block_included_when_feature_id_set(self):
        """When feature_id is set, the feature context block is injected."""
        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch(
                "plugins.tools.workspace.handle",
                return_value=self._make_workspace_result(),
            ),
            patch(
                "plugins.tools.feature.handle",
                return_value=self._make_feature_result(),
            ),
            patch(
                "plugins.tools.tasks.handle",
                return_value={"ok": True, "tasks": []},
            ),
            patch(
                "plugins.feature_context.get_feature_context",
                return_value="## Feature Context (auto-loaded)\n\nMocked block.",
            ),
        ):
            content = self._call_inject()

        assert "## Feature Context (auto-loaded)" in content
        assert "Mocked block." in content

    def test_feature_block_not_injected_when_no_feature_id(self):
        """When feature_id is not set, no feature context block appears."""
        with (
            patch("plugins.hooks.check_workflow_available", return_value=False),
            patch(
                "plugins.feature_context.get_feature_context",
                return_value="SHOULD NOT APPEAR",
            ),
        ):
            content = self._call_inject(feature_id="")

        assert "SHOULD NOT APPEAR" not in content

    def test_feature_context_error_is_non_blocking(self):
        """If get_feature_context raises, inject_context still returns context."""
        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch(
                "plugins.tools.workspace.handle",
                return_value=self._make_workspace_result(),
            ),
            patch(
                "plugins.tools.feature.handle",
                return_value=self._make_feature_result(),
            ),
            patch(
                "plugins.tools.tasks.handle",
                return_value={"ok": True, "tasks": []},
            ),
            patch(
                "plugins.feature_context.get_feature_context",
                side_effect=RuntimeError("test error"),
            ),
        ):
            content = self._call_inject()

        # The context should still contain the base content
        assert "## Workflow context" in content
        # Error should be captured as HTML comment
        assert "<!-- Feature context unavailable" in content
        assert "test error" in content

    def test_empty_feature_context_not_injected(self):
        """When get_feature_context returns empty string, nothing added."""
        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch(
                "plugins.tools.workspace.handle",
                return_value=self._make_workspace_result(),
            ),
            patch(
                "plugins.tools.feature.handle",
                return_value=self._make_feature_result(),
            ),
            patch(
                "plugins.tools.tasks.handle",
                return_value={"ok": True, "tasks": []},
            ),
            patch(
                "plugins.feature_context.get_feature_context",
                return_value="",
            ),
        ):
            content = self._call_inject()

        # The base content should be present, but no empty feature block
        assert "## Workflow context" in content
        # get_feature_context was called but returned empty — block not appended
        # Verify the content ends normally (capabilities section)
        assert "get_tasks (live task status)" in content
