"""Tests for T3 additions to workflow_plugin.

Covers:
  - workflow_get_tasks: parametrisation, happy path, db error
  - gitnexus/rag: arg passing (mock call_mcp_tool)
  - check_available gating: tools omitted when URL env vars not set
  - register(): 7 tools, 2 MCP tools with is_async=True
  - inject_context: task-summary block + capability advertisement
  - inject_context: blocked_tasks block when a task is blocked
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    """Remove workflow_plugin modules between tests to avoid cross-test pollution."""
    keys = [k for k in sys.modules if k.startswith("workflow_plugin")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("workflow_plugin")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_mcp_urls(monkeypatch):
    """Ensure MCP URL env vars are unset by default so check_available returns False."""
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
    yield


# ---------------------------------------------------------------------------
# workflow_get_tasks — parametrisation
# ---------------------------------------------------------------------------


class TestWorkflowGetTasks:
    def test_happy_path_returns_tasks(self):
        fake_tasks = [
            {
                "task_name": "T1",
                "title": "Do something",
                "status": "done",
                "blocked_reason": None,
                "depends_on": [],
                "pr": None,
                "execution": {},
            },
            {
                "task_name": "T2",
                "title": "Do more",
                "status": "in_progress",
                "blocked_reason": None,
                "depends_on": ["T1"],
                "pr": None,
                "execution": {},
            },
        ]
        with patch("workflow_plugin.db.get_feature_tasks", return_value=fake_tasks):
            from workflow_plugin.tools.tasks import handle

            result = handle(workspace_id="ws-1", feature_id="feat-1")
        assert result["ok"] is True
        assert result["tasks"] == fake_tasks

    def test_db_error_returns_ok_false(self):
        with patch(
            "workflow_plugin.db.get_feature_tasks", side_effect=RuntimeError("db down")
        ):
            from workflow_plugin.tools.tasks import handle

            result = handle(workspace_id="ws-1", feature_id="feat-1")
        assert result["ok"] is False
        assert "db down" in result["error"]

    @pytest.mark.parametrize(
        "workspace_id,feature_id",
        [
            ("ws-1", "feat-1"),
            ("my-workspace", "m3-agent-chat-v2"),
            (
                "00000000-0000-0000-0000-000000000001",
                "00000000-0000-0000-0000-000000000002",
            ),
        ],
    )
    def test_passes_args_to_get_feature_tasks(self, workspace_id, feature_id):
        with patch("workflow_plugin.db.get_feature_tasks", return_value=[]) as mock_fn:
            from workflow_plugin.tools.tasks import handle

            handle(workspace_id=workspace_id, feature_id=feature_id)
        mock_fn.assert_called_once_with(workspace_id, feature_id)

    def test_extra_kwargs_ignored(self):
        with patch("workflow_plugin.db.get_feature_tasks", return_value=[]):
            from workflow_plugin.tools.tasks import handle

            result = handle(
                workspace_id="ws-1", feature_id="feat-1", extra_param="ignored"
            )
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# workflow_query_gitnexus — arg passing
# ---------------------------------------------------------------------------


class TestWorkflowQueryGitnexus:
    @pytest.mark.asyncio
    async def test_happy_path_passes_query_and_tool(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        fake_results = [{"type": "text", "text": "symbol found"}]
        with patch(
            "workflow_plugin.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=fake_results,
        ) as mock_call:
            from workflow_plugin.tools.gitnexus import handle

            result = await handle(query="where is register() defined", tool="query")
        assert result["ok"] is True
        assert result["results"] == fake_results
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002/sse",
            "query",
            {"query": "where is register() defined"},
        )

    @pytest.mark.asyncio
    async def test_default_tool_is_query(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "workflow_plugin.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from workflow_plugin.tools.gitnexus import handle

            await handle(query="find X")
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002/sse", "query", {"query": "find X"}
        )

    @pytest.mark.asyncio
    async def test_non_default_tool_forwarded(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "workflow_plugin.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from workflow_plugin.tools.gitnexus import handle

            await handle(query="what calls register()", tool="context")
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002/sse", "context", {"query": "what calls register()"}
        )

    @pytest.mark.asyncio
    async def test_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "workflow_plugin.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            side_effect=ConnectionError("refused"),
        ):
            from workflow_plugin.tools.gitnexus import handle

            result = await handle(query="anything")
        assert result["ok"] is False
        assert "refused" in result["error"]

    def test_check_available_false_when_unset(self):
        from workflow_plugin.tools.gitnexus import check_available

        assert check_available() is False

    def test_check_available_true_when_set(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        from workflow_plugin.tools.gitnexus import check_available

        assert check_available() is True

    def test_check_available_false_for_blank(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "   ")
        from workflow_plugin.tools.gitnexus import check_available

        assert check_available() is False


# ---------------------------------------------------------------------------
# workflow_query_rag — arg passing
# ---------------------------------------------------------------------------


class TestWorkflowQueryRag:
    @pytest.mark.asyncio
    async def test_happy_path_passes_all_args(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        fake_results = [{"type": "text", "text": "matching doc"}]
        with patch(
            "workflow_plugin.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=fake_results,
        ) as mock_call:
            from workflow_plugin.tools.rag import handle

            result = await handle(
                query="prior auth decisions", workspace_id="ws-1", top_k=3
            )
        assert result["ok"] is True
        assert result["results"] == fake_results
        mock_call.assert_awaited_once_with(
            "http://rag:8003/sse",
            "rag_query",
            {"query": "prior auth decisions", "workspace_id": "ws-1", "top_k": 3},
        )

    @pytest.mark.asyncio
    async def test_default_top_k_is_5(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        with patch(
            "workflow_plugin.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from workflow_plugin.tools.rag import handle

            await handle(query="q", workspace_id="ws-1")
        called_args = mock_call.await_args[0]
        assert called_args[2]["top_k"] == 5

    @pytest.mark.asyncio
    async def test_workspace_id_always_forwarded(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        with patch(
            "workflow_plugin.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from workflow_plugin.tools.rag import handle

            await handle(query="q", workspace_id="specific-ws")
        called_args = mock_call.await_args[0]
        assert called_args[2]["workspace_id"] == "specific-ws"

    @pytest.mark.asyncio
    async def test_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        with patch(
            "workflow_plugin.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            side_effect=TimeoutError("timeout"),
        ):
            from workflow_plugin.tools.rag import handle

            result = await handle(query="q", workspace_id="ws-1")
        assert result["ok"] is False
        assert "timeout" in result["error"]

    def test_check_available_false_when_unset(self):
        from workflow_plugin.tools.rag import check_available

        assert check_available() is False

    def test_check_available_true_when_set(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        from workflow_plugin.tools.rag import check_available

        assert check_available() is True


# ---------------------------------------------------------------------------
# check_available gating — tool omitted when URL unset
# ---------------------------------------------------------------------------


class TestCheckAvailableGating:
    def test_gitnexus_excluded_from_definitions_when_url_unset(self):
        """When GITNEXUS_MCP_URL is unset, gitnexus.check_available() returns False."""
        from workflow_plugin.tools.gitnexus import check_available

        assert check_available() is False

    def test_rag_excluded_from_definitions_when_url_unset(self):
        """When RAG_MCP_URL is unset, rag.check_available() returns False."""
        from workflow_plugin.tools.rag import check_available

        assert check_available() is False

    def test_gitnexus_included_when_url_set(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        from workflow_plugin.tools.gitnexus import check_available

        assert check_available() is True

    def test_rag_included_when_url_set(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        from workflow_plugin.tools.rag import check_available

        assert check_available() is True


# ---------------------------------------------------------------------------
# register() — 7 tools, 2 MCP tools with is_async=True
# ---------------------------------------------------------------------------


class TestRegisterT3:
    def test_registers_7_tools(self):
        from workflow_plugin import register, _TOOLS

        ctx = MagicMock()
        register(ctx)
        assert ctx.register_tool.call_count == 7

    def test_all_7_tool_names_registered(self):
        from workflow_plugin import register

        ctx = MagicMock()
        register(ctx)
        names = {
            call.kwargs.get("name") or call.args[0]
            for call in ctx.register_tool.call_args_list
        }
        expected = {
            "workflow_get_workspace_context",
            "workflow_get_feature_state",
            "workflow_write_product_spec",
            "workflow_write_technical_design",
            "workflow_get_tasks",
            "workflow_query_gitnexus",
            "workflow_query_rag",
        }
        assert names == expected

    def test_gitnexus_registered_with_is_async_true(self):
        from workflow_plugin import register

        ctx = MagicMock()
        register(ctx)
        gitnexus_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "workflow_query_gitnexus"
        )
        assert gitnexus_call.kwargs.get("is_async") is True

    def test_rag_registered_with_is_async_true(self):
        from workflow_plugin import register

        ctx = MagicMock()
        register(ctx)
        rag_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "workflow_query_rag"
        )
        assert rag_call.kwargs.get("is_async") is True

    def test_non_mcp_tools_not_async(self):
        from workflow_plugin import register

        ctx = MagicMock()
        register(ctx)
        sync_names = {
            "workflow_get_workspace_context",
            "workflow_get_feature_state",
            "workflow_write_product_spec",
            "workflow_write_technical_design",
            "workflow_get_tasks",
        }
        for call in ctx.register_tool.call_args_list:
            name = call.kwargs.get("name") or call.args[0]
            if name in sync_names:
                assert not call.kwargs.get("is_async"), f"{name} should not be async"

    def test_gitnexus_uses_own_check_fn(self):
        from workflow_plugin import register
        from workflow_plugin.tools import gitnexus

        ctx = MagicMock()
        register(ctx)
        gitnexus_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "workflow_query_gitnexus"
        )
        assert gitnexus_call.kwargs.get("check_fn") is gitnexus.check_available

    def test_rag_uses_own_check_fn(self):
        from workflow_plugin import register
        from workflow_plugin.tools import rag

        ctx = MagicMock()
        register(ctx)
        rag_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "workflow_query_rag"
        )
        assert rag_call.kwargs.get("check_fn") is rag.check_available

    def test_registers_pre_llm_call_hook(self):
        from workflow_plugin import register

        ctx = MagicMock()
        register(ctx)
        ctx.register_hook.assert_called_once()
        hook_name = ctx.register_hook.call_args[0][0]
        assert hook_name == "pre_llm_call"


# ---------------------------------------------------------------------------
# inject_context — task-summary + capability advertisement
# ---------------------------------------------------------------------------


class TestInjectContextT3:
    def _make_fake_workspace_result(self):
        return {"ok": True, "workspace": {"repos": [{"id": "hermes-agent"}]}}

    def _make_fake_feature_result(self):
        return {"ok": True, "feature": {"stage": "in_implementation"}}

    def _make_tasks_with_blocked(self):
        return {
            "ok": True,
            "tasks": [
                {"task_name": "T1", "status": "done", "blocked_reason": None},
                {
                    "task_name": "T2",
                    "status": "blocked",
                    "blocked_reason": "db_unreachable",
                },
                {"task_name": "T3", "status": "in_progress", "blocked_reason": None},
            ],
        }

    def _make_tasks_no_blocked(self):
        return {
            "ok": True,
            "tasks": [
                {"task_name": "T1", "status": "done", "blocked_reason": None},
                {"task_name": "T2", "status": "in_progress", "blocked_reason": None},
            ],
        }

    def test_task_summary_block_injected(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
        from workflow_plugin.hooks import inject_context

        with (
            patch("workflow_plugin.hooks.check_workflow_available", return_value=True),
            patch(
                "workflow_plugin.tools.workspace.handle",
                return_value=self._make_fake_workspace_result(),
            ),
            patch(
                "workflow_plugin.tools.feature.handle",
                return_value=self._make_fake_feature_result(),
            ),
            patch(
                "workflow_plugin.tools.tasks.handle",
                return_value=self._make_tasks_no_blocked(),
            ),
        ):
            messages = []
            inject_context(
                messages, context_vars={"workspace_id": "ws-1", "feature_id": "feat-1"}
            )
        content = messages[0]["content"]
        assert "task_counts:" in content

    def test_blocked_tasks_block_included_when_blocked(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
        from workflow_plugin.hooks import inject_context

        with (
            patch("workflow_plugin.hooks.check_workflow_available", return_value=True),
            patch(
                "workflow_plugin.tools.workspace.handle",
                return_value=self._make_fake_workspace_result(),
            ),
            patch(
                "workflow_plugin.tools.feature.handle",
                return_value=self._make_fake_feature_result(),
            ),
            patch(
                "workflow_plugin.tools.tasks.handle",
                return_value=self._make_tasks_with_blocked(),
            ),
        ):
            messages = []
            inject_context(
                messages, context_vars={"workspace_id": "ws-1", "feature_id": "feat-1"}
            )
        content = messages[0]["content"]
        assert "blocked_tasks:" in content
        assert "db_unreachable" in content

    def test_blocked_tasks_absent_when_none_blocked(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
        from workflow_plugin.hooks import inject_context

        with (
            patch("workflow_plugin.hooks.check_workflow_available", return_value=True),
            patch(
                "workflow_plugin.tools.workspace.handle",
                return_value=self._make_fake_workspace_result(),
            ),
            patch(
                "workflow_plugin.tools.feature.handle",
                return_value=self._make_fake_feature_result(),
            ),
            patch(
                "workflow_plugin.tools.tasks.handle",
                return_value=self._make_tasks_no_blocked(),
            ),
        ):
            messages = []
            inject_context(
                messages, context_vars={"workspace_id": "ws-1", "feature_id": "feat-1"}
            )
        content = messages[0]["content"]
        assert "blocked_tasks:" not in content

    def test_capability_advertisement_includes_workflow_get_tasks(self):
        from workflow_plugin.hooks import inject_context

        with patch(
            "workflow_plugin.hooks.check_workflow_available", return_value=False
        ):
            messages = []
            inject_context(messages, context_vars={"workspace_id": "ws-1"})
        content = messages[0]["content"]
        assert "workflow_get_tasks" in content

    def test_gitnexus_advertised_when_url_set(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        from workflow_plugin.hooks import inject_context

        with patch(
            "workflow_plugin.hooks.check_workflow_available", return_value=False
        ):
            messages = []
            inject_context(messages, context_vars={"workspace_id": "ws-1"})
        content = messages[0]["content"]
        assert "workflow_query_gitnexus" in content

    def test_gitnexus_not_advertised_when_url_unset(self):
        from workflow_plugin.hooks import inject_context

        with patch(
            "workflow_plugin.hooks.check_workflow_available", return_value=False
        ):
            messages = []
            inject_context(messages, context_vars={"workspace_id": "ws-1"})
        content = messages[0]["content"]
        assert "workflow_query_gitnexus" not in content

    def test_rag_advertised_when_url_set(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        from workflow_plugin.hooks import inject_context

        with patch(
            "workflow_plugin.hooks.check_workflow_available", return_value=False
        ):
            messages = []
            inject_context(messages, context_vars={"workspace_id": "ws-1"})
        content = messages[0]["content"]
        assert "workflow_query_rag" in content

    def test_rag_not_advertised_when_url_unset(self):
        from workflow_plugin.hooks import inject_context

        with patch(
            "workflow_plugin.hooks.check_workflow_available", return_value=False
        ):
            messages = []
            inject_context(messages, context_vars={"workspace_id": "ws-1"})
        content = messages[0]["content"]
        assert "workflow_query_rag" not in content

    def test_task_summary_counts_by_status(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
        from workflow_plugin.hooks import inject_context

        tasks_result = {
            "ok": True,
            "tasks": [
                {"task_name": "T1", "status": "done", "blocked_reason": None},
                {"task_name": "T2", "status": "done", "blocked_reason": None},
                {"task_name": "T3", "status": "in_progress", "blocked_reason": None},
            ],
        }
        with (
            patch("workflow_plugin.hooks.check_workflow_available", return_value=True),
            patch(
                "workflow_plugin.tools.workspace.handle",
                return_value=self._make_fake_workspace_result(),
            ),
            patch(
                "workflow_plugin.tools.feature.handle",
                return_value=self._make_fake_feature_result(),
            ),
            patch("workflow_plugin.tools.tasks.handle", return_value=tasks_result),
        ):
            messages = []
            inject_context(
                messages, context_vars={"workspace_id": "ws-1", "feature_id": "feat-1"}
            )
        content = messages[0]["content"]
        assert "done=2" in content
        assert "in_progress=1" in content
