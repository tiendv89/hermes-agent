"""Tests for T3 additions to plugins.

Covers:
  - get_tasks: parametrisation, happy path, db error
  - gitnexus/rag: arg passing (mock call_mcp_tool)
  - check_available gating: tools omitted when URL env vars not set
  - register(): 10 tools total (incl. load_skill + request_approval), 2 MCP tools with is_async=True
  - inject_context: task-summary block + capability advertisement
  - inject_context: blocked_tasks block when a task is blocked
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _load_plugins_register():
    """Load plugins/__init__.py directly from REPO_ROOT to avoid the
    tests/plugins/ shadow package masking the real implementation.
    """
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
    """Remove plugins modules between tests to avoid cross-test pollution."""
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
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
# get_tasks — parametrisation
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
        with patch("plugins.db.get_feature_tasks", return_value=fake_tasks):
            from plugins.tools.tasks import handle

            result = handle(workspace_id="ws-1", feature_id="feat-1")
        assert result["ok"] is True
        assert result["tasks"] == fake_tasks

    def test_db_error_returns_ok_false(self):
        with patch("plugins.db.get_feature_tasks", side_effect=RuntimeError("db down")):
            from plugins.tools.tasks import handle

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
        with patch("plugins.db.get_feature_tasks", return_value=[]) as mock_fn:
            from plugins.tools.tasks import handle

            handle(workspace_id=workspace_id, feature_id=feature_id)
        mock_fn.assert_called_once_with(workspace_id, feature_id)

    def test_extra_kwargs_ignored(self):
        with patch("plugins.db.get_feature_tasks", return_value=[]):
            from plugins.tools.tasks import handle

            result = handle(
                workspace_id="ws-1", feature_id="feat-1", extra_param="ignored"
            )
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# query_gitnexus — arg passing
# ---------------------------------------------------------------------------


class TestWorkflowQueryGitnexus:
    @pytest.mark.asyncio
    async def test_happy_path_passes_query_and_tool(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        fake_results = [{"type": "text", "text": "symbol found"}]
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=fake_results,
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            result = await handle(query="where is register() defined", tool="query")
        assert result["ok"] is True
        assert result["results"] == fake_results
        # GitNexus's `query` tool takes `query` (live contract).
        # workspace_id="" because no session context is set in this test.
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002/sse",
            "query",
            {"query": "where is register() defined"},
            workspace_id="",
        )

    @pytest.mark.asyncio
    async def test_repo_is_forwarded(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            await handle(query="TopNav", tool="query", repo="voyager-interface")
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002/sse",
            "query",
            {"query": "TopNav", "repo": "voyager-interface"},
            workspace_id="",
        )

    @pytest.mark.asyncio
    async def test_impact_passes_target_and_direction(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            await handle(
                query="NotificationBell", tool="impact", repo="voyager-interface"
            )
        # `impact` takes `target` + `direction` (default upstream), not `symbol`.
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002/sse",
            "impact",
            {
                "target": "NotificationBell",
                "direction": "upstream",
                "repo": "voyager-interface",
            },
            workspace_id="",
        )

    @pytest.mark.asyncio
    async def test_default_tool_is_query(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            await handle(query="find X")
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002/sse", "query", {"query": "find X"}, workspace_id=""
        )

    @pytest.mark.asyncio
    async def test_non_default_tool_forwarded(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            await handle(query="register", tool="context")
        # `context` takes `name` (live contract), not `symbol`.
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002/sse", "context", {"name": "register"}, workspace_id=""
        )

    @pytest.mark.asyncio
    async def test_detect_changes_uses_diff_scope_no_query(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            # detect_changes analyzes the git diff; it takes no query/file list.
            result = await handle(tool="detect_changes", repo="voyager-interface")
        assert result["ok"] is True
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002/sse",
            "detect_changes",
            {"scope": "unstaged", "repo": "voyager-interface"},
            workspace_id="",
        )

    @pytest.mark.asyncio
    async def test_list_repos_needs_no_query(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            result = await handle(tool="list_repos")
        assert result["ok"] is True
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002/sse", "list_repos", {}, workspace_id=""
        )

    @pytest.mark.asyncio
    async def test_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            side_effect=ConnectionError("refused"),
        ):
            from plugins.tools.gitnexus import handle

            result = await handle(query="anything")
        assert result["ok"] is False
        assert "refused" in result["error"]

    def test_check_available_false_when_unset(self):
        from plugins.tools.gitnexus import check_available

        assert check_available() is False

    def test_check_available_true_when_set(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        from plugins.tools.gitnexus import check_available

        assert check_available() is True

    def test_check_available_false_for_blank(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "   ")
        from plugins.tools.gitnexus import check_available

        assert check_available() is False


# ---------------------------------------------------------------------------
# query_rag — arg passing
# ---------------------------------------------------------------------------


class TestWorkflowQueryRag:
    @pytest.mark.asyncio
    async def test_happy_path_passes_all_args(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        fake_results = [{"type": "text", "text": "matching doc"}]
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=fake_results,
        ) as mock_call:
            from plugins.tools.rag import handle

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
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.rag import handle

            await handle(query="q", workspace_id="ws-1")
        called_args = mock_call.await_args[0]
        assert called_args[2]["top_k"] == 5

    @pytest.mark.asyncio
    async def test_workspace_id_always_forwarded(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.rag import handle

            await handle(query="q", workspace_id="specific-ws")
        called_args = mock_call.await_args[0]
        assert called_args[2]["workspace_id"] == "specific-ws"

    @pytest.mark.asyncio
    async def test_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            side_effect=TimeoutError("timeout"),
        ):
            from plugins.tools.rag import handle

            result = await handle(query="q", workspace_id="ws-1")
        assert result["ok"] is False
        assert "timeout" in result["error"]

    def test_check_available_false_when_unset(self):
        from plugins.tools.rag import check_available

        assert check_available() is False

    def test_check_available_true_when_set(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        from plugins.tools.rag import check_available

        assert check_available() is True


# ---------------------------------------------------------------------------
# check_available gating — tool omitted when URL unset
# ---------------------------------------------------------------------------


class TestCheckAvailableGating:
    def test_gitnexus_excluded_from_definitions_when_url_unset(self):
        """When GITNEXUS_MCP_URL is unset, gitnexus.check_available() returns False."""
        from plugins.tools.gitnexus import check_available

        assert check_available() is False

    def test_rag_excluded_from_definitions_when_url_unset(self):
        """When RAG_MCP_URL is unset, rag.check_available() returns False."""
        from plugins.tools.rag import check_available

        assert check_available() is False

    def test_gitnexus_included_when_url_set(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        from plugins.tools.gitnexus import check_available

        assert check_available() is True

    def test_rag_included_when_url_set(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        from plugins.tools.rag import check_available

        assert check_available() is True


# ---------------------------------------------------------------------------
# register() — 10 tools total, 2 of them MCP tools with is_async=True
# ---------------------------------------------------------------------------


class TestRegisterT3:
    # The full registered toolset (see plugins/__init__.py _TOOLS).
    EXPECTED_TOOLS = {
        "get_workspace_context",
        "get_feature_state",
        "write_product_spec",
        "read_document",
        "edit_document",
        "write_technical_design",
        "get_tasks",
        "query_gitnexus",
        "query_rag",
        "load_skill",
        "request_approval",
        "approve_feature",
        "write_tasks",
        "suggest_next_actions",
    }

    def test_registers_all_tools(self):
        plugins_mod = _load_plugins_register()
        ctx = MagicMock()
        plugins_mod.register(ctx)
        assert ctx.register_tool.call_count == len(self.EXPECTED_TOOLS)

    def test_all_tool_names_registered(self):
        plugins_mod = _load_plugins_register()
        ctx = MagicMock()
        plugins_mod.register(ctx)
        names = {
            call.kwargs.get("name") or call.args[0]
            for call in ctx.register_tool.call_args_list
        }
        assert names == self.EXPECTED_TOOLS

    def test_gitnexus_registered_with_is_async_true(self):
        plugins_mod = _load_plugins_register()
        ctx = MagicMock()
        plugins_mod.register(ctx)
        gitnexus_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "query_gitnexus"
        )
        assert gitnexus_call.kwargs.get("is_async") is True

    def test_rag_registered_with_is_async_true(self):
        plugins_mod = _load_plugins_register()
        ctx = MagicMock()
        plugins_mod.register(ctx)
        rag_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "query_rag"
        )
        assert rag_call.kwargs.get("is_async") is True

    def test_non_mcp_tools_not_async(self):
        plugins_mod = _load_plugins_register()
        ctx = MagicMock()
        plugins_mod.register(ctx)
        sync_names = {
            "get_workspace_context",
            "get_feature_state",
            "write_product_spec",
            "edit_document",
            "write_technical_design",
            "get_tasks",
            "request_approval",
        }
        for call in ctx.register_tool.call_args_list:
            name = call.kwargs.get("name") or call.args[0]
            if name in sync_names:
                assert not call.kwargs.get("is_async"), f"{name} should not be async"

    def test_registered_handler_returns_json_string(self):
        """The registered tool handler must JSON-stringify the dict return so
        strict OpenAI-compatible providers (DeepSeek) don't reject dict content."""
        import json as _json

        plugins_mod = _load_plugins_register()
        ctx = MagicMock()
        plugins_mod.register(ctx)

        spec_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "write_product_spec"
        )
        wrapped = spec_call.kwargs["handler"]

        # With GITHUB_TOKEN unset the real handler returns an ok:False dict;
        # the wrapper must JSON-stringify it (not pass a dict through).
        out = wrapped(content="x", workspace_id="ws", feature_id="f")
        assert isinstance(out, str)
        assert _json.loads(out)["ok"] is False

    @pytest.mark.asyncio
    async def test_registered_async_handler_returns_json_string(self):
        plugins_mod = _load_plugins_register()
        ctx = MagicMock()
        plugins_mod.register(ctx)

        rag_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "query_rag"
        )
        wrapped = rag_call.kwargs["handler"]
        assert rag_call.kwargs.get("is_async") is True

        # With RAG_MCP_URL unset, the real async handle() returns an ok:False
        # dict; the wrapper must await it and return a JSON string.
        import json as _json

        out = await wrapped(query="q", workspace_id="ws")
        assert isinstance(out, str)
        assert _json.loads(out)["ok"] is False

    def test_gitnexus_uses_own_check_fn(self):
        plugins_mod = _load_plugins_register()
        from plugins.tools import gitnexus

        ctx = MagicMock()
        plugins_mod.register(ctx)
        gitnexus_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "query_gitnexus"
        )
        assert gitnexus_call.kwargs.get("check_fn") is gitnexus.check_available

    def test_rag_uses_own_check_fn(self):
        plugins_mod = _load_plugins_register()
        from plugins.tools import rag

        ctx = MagicMock()
        plugins_mod.register(ctx)
        rag_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "query_rag"
        )
        assert rag_call.kwargs.get("check_fn") is rag.check_available

    def test_registers_pre_llm_call_hook(self):
        plugins_mod = _load_plugins_register()
        ctx = MagicMock()
        plugins_mod.register(ctx)
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

    def _call_inject(self, workspace_id="ws-1", feature_id="feat-1") -> str:
        """Set session context, call inject_context, return the context string."""
        from plugins.context import set_context
        from plugins.hooks import inject_context

        session_id = "sess-test"
        set_context(session_id, workspace_id, feature_id)
        result = inject_context(session_id=session_id)
        return result["context"] if result else ""

    def _make_tasks_with_blocked(self):
        return {
            "ok": True,
            "tasks": [
                {
                    "task_name": "T1",
                    "title": "Setup DB",
                    "status": "done",
                    "blocked_reason": None,
                },
                {
                    "task_name": "T2",
                    "title": "API endpoints",
                    "status": "blocked",
                    "blocked_reason": "db_unreachable",
                },
                {
                    "task_name": "T3",
                    "title": "Frontend",
                    "status": "in_progress",
                    "blocked_reason": None,
                },
            ],
        }

    def _make_tasks_no_blocked(self):
        return {
            "ok": True,
            "tasks": [
                {
                    "task_name": "T1",
                    "title": "Setup DB",
                    "status": "done",
                    "blocked_reason": None,
                },
                {
                    "task_name": "T2",
                    "title": "API endpoints",
                    "status": "in_progress",
                    "blocked_reason": None,
                },
            ],
        }

    def test_task_summary_block_injected(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch(
                "plugins.tools.workspace.handle",
                return_value=self._make_fake_workspace_result(),
            ),
            patch(
                "plugins.tools.feature.handle",
                return_value=self._make_fake_feature_result(),
            ),
            patch(
                "plugins.tools.tasks.handle", return_value=self._make_tasks_no_blocked()
            ),
        ):
            content = self._call_inject()
        assert "tasks:" in content
        assert "T1" in content
        assert "T2" in content

    def test_blocked_tasks_block_included_when_blocked(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch(
                "plugins.tools.workspace.handle",
                return_value=self._make_fake_workspace_result(),
            ),
            patch(
                "plugins.tools.feature.handle",
                return_value=self._make_fake_feature_result(),
            ),
            patch(
                "plugins.tools.tasks.handle",
                return_value=self._make_tasks_with_blocked(),
            ),
        ):
            content = self._call_inject()
        assert "T2" in content
        assert "blocked" in content
        assert "db_unreachable" in content

    def test_blocked_tasks_absent_when_none_blocked(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch(
                "plugins.tools.workspace.handle",
                return_value=self._make_fake_workspace_result(),
            ),
            patch(
                "plugins.tools.feature.handle",
                return_value=self._make_fake_feature_result(),
            ),
            patch(
                "plugins.tools.tasks.handle", return_value=self._make_tasks_no_blocked()
            ),
        ):
            content = self._call_inject()
        assert "blocked:" not in content
        assert "T1" in content
        assert "T2" in content

    def test_capability_advertisement_includes_get_tasks(self):
        with patch("plugins.hooks.check_workflow_available", return_value=False):
            content = self._call_inject(feature_id="")
        assert "get_tasks" in content

    def test_gitnexus_advertised_when_url_set(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch("plugins.hooks.check_workflow_available", return_value=False):
            content = self._call_inject(feature_id="")
        assert "query_gitnexus" in content

    def test_gitnexus_not_advertised_when_url_unset(self):
        with patch("plugins.hooks.check_workflow_available", return_value=False):
            content = self._call_inject(feature_id="")
        assert "query_gitnexus" not in content

    def test_rag_advertised_when_url_set(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        with patch("plugins.hooks.check_workflow_available", return_value=False):
            content = self._call_inject(feature_id="")
        assert "query_rag" in content

    def test_rag_not_advertised_when_url_unset(self):
        with patch("plugins.hooks.check_workflow_available", return_value=False):
            content = self._call_inject(feature_id="")
        assert "query_rag" not in content

    def test_task_summary_lists_all_tasks(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
        tasks_result = {
            "ok": True,
            "tasks": [
                {
                    "task_name": "T1",
                    "title": "Setup DB",
                    "status": "done",
                    "blocked_reason": None,
                },
                {
                    "task_name": "T2",
                    "title": "API layer",
                    "status": "done",
                    "blocked_reason": None,
                },
                {
                    "task_name": "T3",
                    "title": "Frontend",
                    "status": "in_progress",
                    "blocked_reason": None,
                },
            ],
        }
        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch(
                "plugins.tools.workspace.handle",
                return_value=self._make_fake_workspace_result(),
            ),
            patch(
                "plugins.tools.feature.handle",
                return_value=self._make_fake_feature_result(),
            ),
            patch("plugins.tools.tasks.handle", return_value=tasks_result),
        ):
            content = self._call_inject()
        assert "T1" in content and "Setup DB" in content and "done" in content
        assert "T3" in content and "Frontend" in content and "in_progress" in content


# ---------------------------------------------------------------------------
# write_product_spec — content coercion (regression for
# "'dict' object has no attribute 'encode'" over the MCP path)
# ---------------------------------------------------------------------------


class TestMcpArgCoercionAndErrors:
    def test_coerce_text_unwraps_dict_query(self):
        from plugins.mcp_client import coerce_text

        assert coerce_text("auth flow") == "auth flow"
        assert coerce_text({"query": "auth flow"}) == "auth flow"
        assert coerce_text({"q": "x"}) == "x"
        assert coerce_text(None) == ""

    def test_unwrap_exception_drills_into_group(self):
        from plugins.mcp_client import _unwrap_exception

        eg = ExceptionGroup("tg", [ConnectionError("connection refused")])
        leaf = _unwrap_exception(eg)
        assert isinstance(leaf, ConnectionError)
        assert str(leaf) == "connection refused"

    @pytest.mark.asyncio
    async def test_rag_coerces_dict_query_to_string(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003/sse")
        with patch(
            "plugins.tools.rag.call_mcp_tool", new_callable=AsyncMock, return_value=[]
        ) as mock_call:
            from plugins.tools.rag import handle

            # Model passes query as a structured object (the blocker case).
            result = await handle(query={"query": "auth flow"}, workspace_id="ws-1")
        assert result["ok"] is True
        # The forwarded argument must be a plain string, not a dict.
        assert mock_call.await_args[0][2]["query"] == "auth flow"

    @pytest.mark.asyncio
    async def test_gitnexus_coerces_dict_query(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002/sse")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            result = await handle(query={"q": "AIAgent"}, tool="query")
        assert result["ok"] is True
        assert mock_call.await_args[0][2] == {"query": "AIAgent"}

    @pytest.mark.asyncio
    async def test_call_mcp_tool_unwraps_transport_taskgroup(self, monkeypatch):
        """When the SSE transport fails with an ExceptionGroup, call_mcp_tool must
        raise a clean MCPCallError carrying the real cause — not the opaque
        'unhandled errors in a TaskGroup'."""
        from plugins import mcp_client

        class _FailingCM:
            async def __aenter__(self):
                raise ExceptionGroup("tg", [ConnectionError("connection refused")])

            async def __aexit__(self, *_a):
                return False

        monkeypatch.setattr(mcp_client, "sse_client", lambda *_a, **_k: _FailingCM())

        with pytest.raises(mcp_client.MCPCallError) as ei:
            await mcp_client.call_mcp_tool("http://gitnexus:8002", "query", {"q": "x"})
        msg = str(ei.value)
        assert "connection refused" in msg
        assert "TaskGroup" not in msg


class TestResolveManagementRepo:
    def test_resolves_from_workspace_context(self):
        from plugins.tools.artifacts import _resolve_management_repo

        ctx = {
            "management_repo": "management-repo",
            "repos": [{"id": "management-repo", "github": "git@github.com:org/ws.git"}],
        }
        assert _resolve_management_repo(ctx) == ("org", "ws")

    def test_env_fallback_when_no_repo_configured(self, monkeypatch):
        from plugins.tools.artifacts import _resolve_management_repo

        monkeypatch.setenv("MANAGEMENT_REPO_GITHUB", "git@github.com:org/ws.git")
        # repos == [] (the blocker case) → env override resolves it.
        assert _resolve_management_repo(
            {"management_repo": "management-repo", "repos": []}
        ) == ("org", "ws")

    def test_env_fallback_accepts_owner_repo_form(self, monkeypatch):
        from plugins.tools.artifacts import _resolve_management_repo

        monkeypatch.setenv("MANAGEMENT_REPO_GITHUB", "org/ws")
        assert _resolve_management_repo(
            {"management_repo": "management-repo", "repos": []}
        ) == ("org", "ws")

    def test_raises_when_unresolvable(self, monkeypatch):
        from plugins.tools.artifacts import _resolve_management_repo

        monkeypatch.delenv("MANAGEMENT_REPO_GITHUB", raising=False)
        with pytest.raises(ValueError, match="MANAGEMENT_REPO_GITHUB"):
            _resolve_management_repo(
                {"management_repo": "management-repo", "repos": []}
            )


class TestWriteArtifactCoercesContent:
    def test_coerce_passes_through_str(self):
        from plugins.tools.artifacts import _coerce_content

        assert _coerce_content("# Spec") == "# Spec"

    def test_coerce_dict_to_json_string(self):
        from plugins.tools.artifacts import _coerce_content

        out = _coerce_content({"title": "Spec", "body": "x"})
        assert isinstance(out, str)
        assert '"title"' in out

    def test_write_product_spec_with_dict_content_does_not_raise(self, monkeypatch):
        """A dict content must be serialized to str before the write, not crash."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        captured = {}

        def fake_write(
            owner, repo, feature_id, base_branch, path, content, sha, message, token
        ):
            captured["content"] = content
            return {"commit_sha": "deadbeef", "pr": {"url": "http://pr"}}

        with (
            patch("plugins.tools.artifacts.get_workspace_context", return_value={}),
            patch(
                "plugins.tools.artifacts._resolve_management_repo",
                return_value=("o", "r"),
            ),
            patch(
                "plugins.tools.artifacts.get_feature_detail",
                return_value={"init_pr_url": None, "owner": "ts"},
            ),
            patch(
                "plugins.tools.artifacts._resolve_document_branch",
                return_value=("feature/feat-1", None),
            ),
            patch(
                "plugins.tools.artifacts.read_document",
                return_value={"content": "", "sha": "base"},
            ),
            patch("plugins.tools.artifacts.write_document", side_effect=fake_write),
            patch("plugins.tools.approval.handle", return_value={"ok": False}),
        ):
            from plugins.tools.artifacts import handle_write_product_spec

            result = handle_write_product_spec(
                content={"title": "My Spec"}, workspace_id="ws-1", feature_id="feat-1"
            )

        assert result["ok"] is True
        assert isinstance(captured["content"], str)
        assert "My Spec" in captured["content"]


# ---------------------------------------------------------------------------
# GitNexus repo-name parsing + write_tasks repo validation guardrail
# ---------------------------------------------------------------------------


class TestGitnexusRepoNames:
    def test_parse_repo_names_ignores_trailing_prose(self):
        from plugins.tools.gitnexus import _parse_repo_names

        # list_repos returns a JSON array followed by a human-readable footer.
        text = (
            '[\n  {"name": "voyager-interface", "path": "/x"},\n'
            '  {"name": "voyager-backend"}\n]\n\n'
            "READ gitnexus://repo/{name}/context for any repo above."
        )
        assert _parse_repo_names([{"type": "text", "text": text}]) == [
            "voyager-interface",
            "voyager-backend",
        ]


class TestWriteTasksRepoValidation:
    def test_rejects_repo_not_indexed_in_gitnexus(self, monkeypatch):
        from plugins.tools import gitnexus

        monkeypatch.setattr(
            gitnexus,
            "list_indexed_repos",
            lambda *a, **k: ["voyager-interface", "voyager-backend"],
        )
        from plugins.tools.tasks_write import handle

        result = handle(
            tasks=[{"id": "T1", "repo": "made-up-repo", "title": "x"}],
            tasks_md="# tasks",
            workspace_id="ws-1",
            feature_id="FARO-1",
        )
        assert result["ok"] is False
        assert "not indexed in GitNexus" in result["error"]

    def test_allows_repo_present_in_gitnexus(self, monkeypatch):
        from plugins.tools import gitnexus

        monkeypatch.setattr(
            gitnexus, "list_indexed_repos", lambda *a, **k: ["voyager-interface"]
        )
        # No token: the call fails AFTER the repo guard — proving a known repo
        # passes repo validation (error is not the repo-validation error).
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from plugins.tools.tasks_write import handle

        result = handle(
            tasks=[{"id": "T1", "repo": "voyager-interface", "title": "x"}],
            tasks_md="# tasks",
            workspace_id="ws-1",
            feature_id="FARO-1",
        )
        assert result["ok"] is False
        assert "not indexed in GitNexus" not in (result.get("error") or "")

    def test_skips_validation_when_gitnexus_unavailable(self, monkeypatch):
        from plugins.tools import gitnexus

        monkeypatch.setattr(gitnexus, "list_indexed_repos", lambda *a, **k: None)
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from plugins.tools.tasks_write import handle

        # Unknown repo, but GitNexus unavailable -> no repo validation error.
        result = handle(
            tasks=[{"id": "T1", "repo": "whatever", "title": "x"}],
            tasks_md="# tasks",
            workspace_id="ws-1",
            feature_id="FARO-1",
        )
        assert result["ok"] is False
        assert "not indexed in GitNexus" not in (result.get("error") or "")
