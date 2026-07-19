"""Tests for the plugins package — tool schemas, registration, hooks,
GitNexus/RAG MCP tool arg-passing and workspace scoping, and the skills
subsystem (local-bundle loader).

Merged from the former test_workflow_plugin_t3/t6/t7.py files.

Covers:
  - register(): all tools registered on a mock PluginContext, MCP tools
    marked is_async=True, JSON-string-wrapped handlers, pre_llm_call hook
  - get_tasks: parametrisation, happy path, db error
  - query_gitnexus / query_rag: arg passing, tool selection, error handling,
    dict-arg coercion, multi-org workspace-vs-session org resolution
  - GitNexus/RAG workspace scoping: _sse_endpoint URL construction,
    call_mcp_tool workspace/org forwarding, gitnexus.handle() context
    resolution, list_indexed_repos() per-workspace cache partitioning,
    resolve_workspace_slug() UUID-to-slug threading
  - check_available gating: tools omitted when their MCP URL env var is unset
  - inject_context: task-summary block, blocked-tasks block, capability
    advertisement, GitNexus workspace-scoped repo listing, skills block
  - write_product_spec / write_tasks: content coercion, repo validation
    against the GitNexus index
  - Skills subsystem (plugins/skills/index.py): SkillEntry, frontmatter
    description parsing, build_index/get_index caching, get_shared_rules,
    directory walking, load_skill tool handler, _skills_for_repos stack
    matching, _build_skills_block formatting
"""

from __future__ import annotations

import importlib.util
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
    """Remove plugins modules between tests to avoid cross-test pollution
    (module-level caches like the skill index are reset per test)."""
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Ensure MCP/workflow-backend URL env vars are unset by default so
    check_available() gates return False unless a test opts in."""
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
    yield


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


def _write_skill(skill_dir: Path, description: str | None) -> None:
    """Write a minimal SKILL.md into *skill_dir* (description optional)."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    if description is None:
        body = f"---\nname: {skill_dir.name}\n---\n# {skill_dir.name}"
    else:
        body = f"---\nname: {skill_dir.name}\ndescription: {description}\n---\n# {skill_dir.name}"
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# register() — 10 tools total, 2 of them MCP tools with is_async=True
# ---------------------------------------------------------------------------


class TestRegisterT3:
    # The full registered toolset (see plugins/__init__.py _TOOLS).
    EXPECTED_TOOLS = {
        "get_workspace_context",
        "get_feature_state",
        "write_product_spec",
        "read_file",
        "read_workspace_file",
        "edit_document",
        "edit_file",
        "write_file",
        "write_technical_design",
        "get_tasks",
        "query_gitnexus",
        "query_rag",
        "load_skill",
        "request_approval",
        "approve_feature",
        "move_feature_status",
        "write_tasks",
        "create_tasks",
        "parse_tasks",
        "suggest_next_actions",
        "github_pr_context",
        "github_pr_review",
        "list_documents",
        "workflow_init_feature",
        "workflow_lookup_feature",
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
        with patch("src.services.workflow_backend_client.get_feature_tasks", AsyncMock(return_value=fake_tasks)):
            from plugins.tools.tasks import handle

            result = handle(workspace_id="ws-1", feature_id="feat-1")
        assert result["ok"] is True
        assert result["tasks"] == fake_tasks

    def test_db_error_returns_ok_false(self):
        with patch(
            "src.services.workflow_backend_client.get_feature_tasks",
            AsyncMock(side_effect=RuntimeError("db down")),
        ):
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
        mock_fn = AsyncMock(return_value=[])
        with patch("src.services.workflow_backend_client.get_feature_tasks", mock_fn):
            from plugins.tools.tasks import handle

            handle(workspace_id=workspace_id, feature_id=feature_id)
        assert mock_fn.call_args.args == (workspace_id, feature_id)

    def test_extra_kwargs_ignored(self):
        with patch("src.services.workflow_backend_client.get_feature_tasks", AsyncMock(return_value=[])):
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
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        fake_results = [{"type": "text", "text": "symbol found"}]
        import plugins.context as ctx

        ctx.set_context("sess-t3-1", "test-workspace", "", org_id="test-org")
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
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002",
            "query",
            {"query": "where is register() defined"},
            workspace_id="test-workspace",
            organization_id="test-org",
            api_key="",
        )

    @pytest.mark.asyncio
    async def test_repo_is_forwarded(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        import plugins.context as ctx

        ctx.set_context("sess-t3-2", "test-workspace", "", org_id="test-org")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            await handle(query="TopNav", tool="query", repo="voyager-interface")
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002",
            "query",
            {"query": "TopNav", "repo": "voyager-interface"},
            workspace_id="test-workspace",
            organization_id="test-org",
            api_key="",
        )

    @pytest.mark.asyncio
    async def test_impact_passes_target_and_direction(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        import plugins.context as ctx

        ctx.set_context("sess-t3-3", "test-workspace", "", org_id="test-org")
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
            "http://gitnexus:8002",
            "impact",
            {
                "target": "NotificationBell",
                "direction": "upstream",
                "repo": "voyager-interface",
            },
            workspace_id="test-workspace",
            organization_id="test-org",
            api_key="",
        )

    @pytest.mark.asyncio
    async def test_default_tool_is_query(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        import plugins.context as ctx

        ctx.set_context("sess-t3-4", "test-workspace", "", org_id="test-org")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            await handle(query="find X")
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002",
            "query",
            {"query": "find X"},
            workspace_id="test-workspace",
            organization_id="test-org",
            api_key="",
        )

    @pytest.mark.asyncio
    async def test_non_default_tool_forwarded(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        import plugins.context as ctx

        ctx.set_context("sess-t3-5", "test-workspace", "", org_id="test-org")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            await handle(query="register", tool="context")
        # `context` takes `name` (live contract), not `symbol`.
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002",
            "context",
            {"name": "register"},
            workspace_id="test-workspace",
            organization_id="test-org",
            api_key="",
        )

    @pytest.mark.asyncio
    async def test_detect_changes_uses_diff_scope_no_query(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        import plugins.context as ctx

        ctx.set_context("sess-t3-6", "test-workspace", "", org_id="test-org")
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
            "http://gitnexus:8002",
            "detect_changes",
            {"scope": "unstaged", "repo": "voyager-interface"},
            workspace_id="test-workspace",
            organization_id="test-org",
            api_key="",
        )

    @pytest.mark.asyncio
    async def test_list_repos_needs_no_query(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        import plugins.context as ctx

        ctx.set_context("sess-t3-7", "test-workspace", "", org_id="test-org")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.gitnexus import handle

            result = await handle(tool="list_repos")
        assert result["ok"] is True
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002",
            "list_repos",
            {},
            workspace_id="test-workspace",
            organization_id="test-org",
            api_key="",
        )

    @pytest.mark.asyncio
    async def test_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        import plugins.context as ctx

        ctx.set_context("sess-t3-8", "test-workspace", "", org_id="test-org")
        with patch(
            "plugins.tools.gitnexus.call_mcp_tool",
            new_callable=AsyncMock,
            side_effect=ConnectionError("refused"),
        ):
            from plugins.tools.gitnexus import handle

            result = await handle(query="anything")
        assert result["ok"] is False
        assert "refused" in result["error"]

    @pytest.mark.asyncio
    async def test_resolves_org_from_workspace_not_session_context(self, monkeypatch):
        """A multi-org user's session "current" org can differ from the org
        that actually owns the workspace being queried — the workspace's
        owning org must win, not the stale session context."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        import plugins.context as ctx

        ctx.set_context("sess-t3-9", "test-workspace", "", org_id="session-org")
        with (
            patch(
                "src.services.workflow_backend_client.get_workspace_organization_id",
                AsyncMock(return_value="workspace-owning-org"),
            ),
            patch(
                "plugins.tools.gitnexus.call_mcp_tool",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_call,
        ):
            from plugins.tools.gitnexus import handle

            result = await handle(query="find X")
        assert result["ok"] is True
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002",
            "query",
            {"query": "find X"},
            workspace_id="test-workspace",
            organization_id="workspace-owning-org",
            api_key="",
        )

    @pytest.mark.asyncio
    async def test_falls_back_to_session_org_when_workspace_lookup_fails(self, monkeypatch):
        """workflow-backend being unreachable must not hard-fail the query —
        fall back to the session's org context."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        import plugins.context as ctx

        ctx.set_context("sess-t3-10", "test-workspace", "", org_id="session-org")
        with (
            patch(
                "src.services.workflow_backend_client.get_workspace_organization_id",
                AsyncMock(side_effect=RuntimeError("workflow-backend unreachable")),
            ),
            patch(
                "plugins.tools.gitnexus.call_mcp_tool",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_call,
        ):
            from plugins.tools.gitnexus import handle

            result = await handle(query="find X")
        assert result["ok"] is True
        mock_call.assert_awaited_once_with(
            "http://gitnexus:8002",
            "query",
            {"query": "find X"},
            workspace_id="test-workspace",
            organization_id="session-org",
            api_key="",
        )

    def test_check_available_false_when_unset(self):
        from plugins.tools.gitnexus import check_available

        assert check_available() is False

    def test_check_available_true_when_set(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        from plugins.tools.gitnexus import check_available

        assert check_available() is True

    def test_check_available_false_for_blank(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "   ")
        from plugins.tools.gitnexus import check_available

        assert check_available() is False

# ---------------------------------------------------------------------------
# _sse_endpoint — URL construction
# ---------------------------------------------------------------------------


class TestSseEndpoint:
    def test_bare_host_no_workspace_leaves_path_unchanged(self):
        from plugins.clients.mcp_client import _sse_endpoint

        assert _sse_endpoint("https://rag.example.com") == "https://rag.example.com"

    def test_bare_host_with_workspace_scopes_to_ws_path(self):
        from plugins.clients.mcp_client import _sse_endpoint

        result = _sse_endpoint("https://rag.example.com", workspace_id="my-ws")
        assert result == "https://rag.example.com/ws/my-ws/sse"

    def test_root_path_no_workspace_leaves_path_unchanged(self):
        from plugins.clients.mcp_client import _sse_endpoint

        assert _sse_endpoint("http://gitnexus:8002/") == "http://gitnexus:8002/"

    def test_root_path_with_workspace_uses_ws_path(self):
        from plugins.clients.mcp_client import _sse_endpoint

        result = _sse_endpoint(
            "http://gitnexus:8002/", workspace_id="project-workspace"
        )
        assert result == "http://gitnexus:8002/ws/project-workspace/sse"

    def test_explicit_path_no_workspace_passed_through_verbatim(self):
        from plugins.clients.mcp_client import _sse_endpoint

        # Without a workspace_id the configured URL is used verbatim — the
        # helper never invents a path.
        assert _sse_endpoint("http://host:8002/custom") == "http://host:8002/custom"

    def test_explicit_path_with_workspace_replaced(self):
        from plugins.clients.mcp_client import _sse_endpoint

        # When workspace_id is given, always use the scoped path — a path left
        # in the env var must not bypass workspace scoping.
        result = _sse_endpoint("http://gitnexus:8002/custom", workspace_id="faro")
        assert result == "http://gitnexus:8002/ws/faro/sse"

    def test_empty_workspace_id_leaves_path_unchanged(self):
        from plugins.clients.mcp_client import _sse_endpoint

        assert (
            _sse_endpoint("https://rag.example.com", workspace_id="")
            == "https://rag.example.com"
        )

    def test_different_workspaces_produce_different_urls(self):
        from plugins.clients.mcp_client import _sse_endpoint

        url_a = _sse_endpoint("http://gitnexus:8002", workspace_id="workspace-a")
        url_b = _sse_endpoint("http://gitnexus:8002", workspace_id="workspace-b")
        assert url_a != url_b
        assert "workspace-a" in url_a
        assert "workspace-b" in url_b

    def test_workspace_id_preserves_query_params(self):
        from plugins.clients.mcp_client import _sse_endpoint

        result = _sse_endpoint("http://host:8000?token=abc", workspace_id="ws1")
        assert "/ws/ws1/sse" in result
        assert "token=abc" in result

    def test_organization_id_and_workspace_id_scope_to_two_segment_path(self):
        from plugins.clients.mcp_client import _sse_endpoint

        result = _sse_endpoint(
            "https://rag.example.com", workspace_id="my-ws", organization_id="my-org"
        )
        assert result == "https://rag.example.com/ws/my-org/my-ws/sse"

    def test_organization_id_without_workspace_id_is_ignored(self):
        """organization_id alone (no workspace_id) must not scope the path —
        rag-service's SSE endpoint always requires both segments together."""
        from plugins.clients.mcp_client import _sse_endpoint

        result = _sse_endpoint("https://rag.example.com", organization_id="my-org")
        assert result == "https://rag.example.com"

# ---------------------------------------------------------------------------
# call_mcp_tool — passes workspace_id through
# ---------------------------------------------------------------------------


class TestCallMcpToolWorkspaceId:
    @pytest.mark.asyncio
    async def test_no_workspace_id_passes_empty_string_to_sse_endpoint(self):
        from plugins.clients.mcp_client import call_mcp_tool

        with (
            patch(
                "plugins.clients.mcp_client._sse_endpoint", return_value="http://host"
            ) as mock_ep,
            patch("plugins.clients.mcp_client.sse_client") as mock_sse,
        ):
            mock_session = AsyncMock()
            mock_session.initialize = AsyncMock()
            mock_session.call_tool = AsyncMock(return_value=MagicMock(content=[]))
            mock_sse.return_value.__aenter__ = AsyncMock(
                return_value=(AsyncMock(), AsyncMock())
            )
            mock_sse.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("plugins.clients.mcp_client.ClientSession") as mock_cs:
                mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
                await call_mcp_tool("http://host", "rag_query", {"query": "test"})

        mock_ep.assert_called_once_with("http://host", "", "")

    @pytest.mark.asyncio
    async def test_workspace_id_forwarded_to_sse_endpoint(self):
        from plugins.clients.mcp_client import call_mcp_tool

        with (
            patch(
                "plugins.clients.mcp_client._sse_endpoint",
                return_value="http://host/ws/ws1/sse",
            ) as mock_ep,
            patch("plugins.clients.mcp_client.sse_client") as mock_sse,
        ):
            mock_session = AsyncMock()
            mock_session.initialize = AsyncMock()
            mock_session.call_tool = AsyncMock(return_value=MagicMock(content=[]))
            mock_sse.return_value.__aenter__ = AsyncMock(
                return_value=(AsyncMock(), AsyncMock())
            )
            mock_sse.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("plugins.clients.mcp_client.ClientSession") as mock_cs:
                mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
                await call_mcp_tool("http://host", "list_repos", {}, workspace_id="ws1")

        mock_ep.assert_called_once_with("http://host", "ws1", "")

    @pytest.mark.asyncio
    async def test_organization_id_forwarded_to_sse_endpoint(self):
        from plugins.clients.mcp_client import call_mcp_tool

        with (
            patch(
                "plugins.clients.mcp_client._sse_endpoint",
                return_value="http://host/ws/org1/ws1/sse",
            ) as mock_ep,
            patch("plugins.clients.mcp_client.sse_client") as mock_sse,
        ):
            mock_session = AsyncMock()
            mock_session.initialize = AsyncMock()
            mock_session.call_tool = AsyncMock(return_value=MagicMock(content=[]))
            mock_sse.return_value.__aenter__ = AsyncMock(
                return_value=(AsyncMock(), AsyncMock())
            )
            mock_sse.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("plugins.clients.mcp_client.ClientSession") as mock_cs:
                mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
                await call_mcp_tool(
                    "http://host",
                    "rag_query",
                    {},
                    workspace_id="ws1",
                    organization_id="org1",
                )

        mock_ep.assert_called_once_with("http://host", "ws1", "org1")

# ---------------------------------------------------------------------------
# gitnexus.handle() — resolves workspace from context
# ---------------------------------------------------------------------------


class TestGitnexusHandleWorkspaceScoping:
    @pytest.mark.asyncio
    async def test_handle_resolves_workspace_from_context(self, monkeypatch):
        """handle() reads workspace_id from session context and passes it to call_mcp_tool."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.context as ctx

        ctx.set_context("sess-a", "workspace-a", "feat-1", org_id="test-org")

        from plugins.tools.gitnexus import handle

        with patch(
            "plugins.tools.gitnexus.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = [{"type": "text", "text": "result"}]
            result = await handle(query="TopNav", tool="query")

        assert result["ok"] is True
        _, kwargs = mock_call.call_args
        assert kwargs.get("workspace_id") == "workspace-a"

    @pytest.mark.asyncio
    async def test_handle_no_workspace_context_returns_error(self, monkeypatch):
        """Without context, workspace_id is empty — returns a clear error instead of
        attempting an unscoped connection."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.context as ctx

        ctx._local.workspace_id = ""

        from plugins.tools.gitnexus import handle

        with patch(
            "plugins.tools.gitnexus.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            result = await handle(query="Symbol", tool="query")

        assert result["ok"] is False
        assert "workspace_id is required" in result["error"]
        mock_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_workspace_a_call_does_not_use_workspace_b_endpoint(
        self, monkeypatch
    ):
        """G2 isolation: workspace A's call never targets workspace B's endpoint."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.context as ctx

        ctx.set_context("sess-a", "workspace-a", "feat-1", org_id="test-org")

        from plugins.tools.gitnexus import handle

        captured_workspace_ids = []

        async def fake_call(url, tool, args, workspace_id="", organization_id="", api_key=""):
            captured_workspace_ids.append(workspace_id)
            return [{"type": "text", "text": "data"}]

        with patch("plugins.tools.gitnexus.call_mcp_tool", side_effect=fake_call):
            await handle(query="Symbol", tool="query")

        assert len(captured_workspace_ids) == 1
        assert captured_workspace_ids[0] == "workspace-a"
        assert "workspace-b" not in captured_workspace_ids[0]

    @pytest.mark.asyncio
    async def test_list_repos_operation_passes_workspace_id(self, monkeypatch):
        """tool='list_repos' also gets workspace_id so it only returns repos for that workspace."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.context as ctx

        ctx.set_context("sess-x", "my-workspace", "", org_id="test-org")

        from plugins.tools.gitnexus import handle

        with patch(
            "plugins.tools.gitnexus.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = []
            await handle(query="", tool="list_repos")

        _, kwargs = mock_call.call_args
        assert kwargs.get("workspace_id") == "my-workspace"

# ---------------------------------------------------------------------------
# list_indexed_repos() — per-workspace cache partitioning
# ---------------------------------------------------------------------------


class TestListIndexedReposWorkspaceScoping:
    def test_workspace_id_passed_to_call_mcp_tool(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.tools.gitnexus as gn_mod

        gn_mod._repo_cache.clear()

        with patch(
            "plugins.tools.gitnexus.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = [{"type": "text", "text": '[{"name":"repo-a"}]'}]
            from plugins.tools.gitnexus import list_indexed_repos

            result = list_indexed_repos(workspace_id="ws-alpha", organization_id="test-org")

        assert result == ["repo-a"]
        _, kwargs = mock_call.call_args
        assert kwargs.get("workspace_id") == "ws-alpha"

    def test_different_workspaces_use_separate_caches(self, monkeypatch):
        """Cache is partitioned by workspace_id: ws-a and ws-b don't share results."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.tools.gitnexus as gn_mod

        gn_mod._repo_cache.clear()

        call_count = {"n": 0}
        responses = {
            "ws-a": [{"type": "text", "text": '[{"name":"repo-a"}]'}],
            "ws-b": [{"type": "text", "text": '[{"name":"repo-b"}]'}],
        }

        async def fake_call(url, tool, args, workspace_id="", organization_id="", api_key=""):
            call_count["n"] += 1
            return responses.get(workspace_id, [])

        with patch("plugins.tools.gitnexus.call_mcp_tool", side_effect=fake_call):
            from plugins.tools.gitnexus import list_indexed_repos

            repos_a = list_indexed_repos(workspace_id="ws-a", organization_id="test-org")
            repos_b = list_indexed_repos(workspace_id="ws-b", organization_id="test-org")

        assert repos_a == ["repo-a"]
        assert repos_b == ["repo-b"]
        assert repos_a != repos_b

    def test_cache_hit_for_same_workspace_does_not_re_call(self, monkeypatch):
        """Second call for same workspace hits cache — no second MCP round-trip."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.tools.gitnexus as gn_mod

        gn_mod._repo_cache.clear()

        with patch(
            "plugins.tools.gitnexus.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = [{"type": "text", "text": '[{"name":"repo-x"}]'}]
            from plugins.tools.gitnexus import list_indexed_repos

            list_indexed_repos(workspace_id="cached-ws", organization_id="test-org")
            list_indexed_repos(workspace_id="cached-ws", organization_id="test-org")

        assert mock_call.call_count == 1

    def test_cache_miss_for_different_workspace_triggers_new_call(self, monkeypatch):
        """Different workspace_id bypasses the cache and makes a fresh MCP call."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.tools.gitnexus as gn_mod

        gn_mod._repo_cache.clear()

        with patch(
            "plugins.tools.gitnexus.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = [{"type": "text", "text": '[{"name":"repo-x"}]'}]
            from plugins.tools.gitnexus import list_indexed_repos

            list_indexed_repos(workspace_id="ws-1", organization_id="test-org")
            list_indexed_repos(workspace_id="ws-2", organization_id="test-org")

        assert mock_call.call_count == 2

    def test_no_url_returns_none_regardless_of_workspace(self):
        """When GITNEXUS_MCP_URL is not set, always return None."""
        from plugins.tools.gitnexus import list_indexed_repos

        assert list_indexed_repos(workspace_id="any-ws") is None

    def test_no_workspace_id_skips_lookup(self, monkeypatch):
        """GitNexus only serves workspace-scoped endpoints — without a
        workspace_id the lookup is skipped entirely (returns None, no MCP
        call attempted)."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.tools.gitnexus as gn_mod

        gn_mod._repo_cache.clear()

        with patch(
            "plugins.tools.gitnexus.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            from plugins.tools.gitnexus import list_indexed_repos

            result = list_indexed_repos()

        assert result is None
        mock_call.assert_not_awaited()

# ---------------------------------------------------------------------------
# db.resolve_workspace_slug() — normalizes slug-or-UUID to the canonical slug
# (shared by gitnexus.py and rag.py)
# ---------------------------------------------------------------------------


class TestRagDoesNotResolveWorkspaceSlug:
    # GitNexus scoping no longer resolves a UUID workspace_id to a slug —
    # gitnexus.py now keys strictly on the raw workspace_id/organization_id
    # (see its handle()/list_indexed_repos() docstrings: "no slug resolution,
    # GitNexus is keyed by the raw workspace_id UUID same as organization_id").
    # This class only covers RAG's (deliberately different) behavior: it must
    # NOT resolve to a slug, since rag-service's Qdrant collections are keyed
    # by the raw UUIDs storage-service uses.

    @pytest.mark.asyncio
    async def test_rag_handle_does_not_resolve_workspace_to_slug(self, monkeypatch):
        """rag.handle() must forward the raw workspace_id unchanged — unlike
        GitNexus, rag-service's Qdrant collections are keyed by the raw
        organization_id/workspace_id UUIDs storage-service uses, not a slug,
        so resolving to a slug here would silently mismatch every collection."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8000")
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        import plugins.context as ctx

        ctx.set_context(
            "sess-rag-uuid",
            "22222222-2222-2222-2222-222222222222",
            "",
            org_id="org-uuid",
        )

        from plugins.tools.rag import handle

        with (
            patch(
                "src.services.workflow_backend_client.get_workspace_slug",
                AsyncMock(return_value="rag-workspace-slug"),
            ),
            patch(
                "plugins.tools.rag.call_mcp_tool", new_callable=AsyncMock
            ) as mock_call,
        ):
            mock_call.return_value = []
            await handle(query="auth flow")

        args, _ = mock_call.call_args
        tool_arguments = args[2] if len(args) > 2 else {}
        assert (
            tool_arguments.get("workspace_id")
            == "22222222-2222-2222-2222-222222222222"
        )

    @pytest.mark.asyncio
    async def test_rag_handle_no_workflow_db_passes_raw_value(self, monkeypatch):
        """Without the workflow backend configured, rag.handle() still forwards the
        raw value (no regression for deployments without a workflow backend)."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8000")

        import plugins.context as ctx

        ctx.set_context("sess-rag-raw", "raw-workspace", "", org_id="raw-org")

        from plugins.tools.rag import handle

        with patch(
            "plugins.tools.rag.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = []
            await handle(query="auth flow")

        args, _ = mock_call.call_args
        tool_arguments = args[2] if len(args) > 2 else {}
        assert tool_arguments.get("workspace_id") == "raw-workspace"
        assert tool_arguments.get("organization_id") == "raw-org"

# ---------------------------------------------------------------------------
# query_rag — arg passing
# ---------------------------------------------------------------------------


class TestWorkflowQueryRag:
    @pytest.mark.asyncio
    async def test_happy_path_passes_all_args(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        fake_results = [{"type": "text", "text": "matching doc"}]
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=fake_results,
        ) as mock_call:
            from plugins.tools.rag import handle

            result = await handle(
                query="prior auth decisions",
                workspace_id="ws-1",
                organization_id="org-1",
                top_k=3,
            )
        assert result["ok"] is True
        assert result["results"] == fake_results
        mock_call.assert_awaited_once_with(
            "http://rag:8003",
            "rag_query",
            {
                "query": "prior auth decisions",
                "organization_id": "org-1",
                "workspace_id": "ws-1",
                "top_k": 3,
            },
            workspace_id="ws-1",
            organization_id="org-1",
            api_key="",
        )

    @pytest.mark.asyncio
    async def test_default_top_k_is_5(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.rag import handle

            await handle(query="q", workspace_id="ws-1", organization_id="org-1")
        called_args = mock_call.await_args[0]
        assert called_args[2]["top_k"] == 5

    @pytest.mark.asyncio
    async def test_workspace_id_always_forwarded(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.rag import handle

            await handle(
                query="q", workspace_id="specific-ws", organization_id="org-1"
            )
        called_args = mock_call.await_args[0]
        assert called_args[2]["workspace_id"] == "specific-ws"

    @pytest.mark.asyncio
    async def test_organization_id_always_forwarded(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.rag import handle

            await handle(
                query="q", workspace_id="ws-1", organization_id="specific-org"
            )
        called_args = mock_call.await_args[0]
        assert called_args[2]["organization_id"] == "specific-org"

    @pytest.mark.asyncio
    async def test_feature_name_forwarded_when_provided(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.rag import handle

            await handle(
                query="q",
                workspace_id="ws-1",
                organization_id="org-1",
                feature_name="checkout-flow",
            )
        called_args = mock_call.await_args[0]
        assert called_args[2]["feature_name"] == "checkout-flow"

    @pytest.mark.asyncio
    async def test_feature_name_omitted_when_not_provided(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.rag import handle

            await handle(query="q", workspace_id="ws-1", organization_id="org-1")
        called_args = mock_call.await_args[0]
        assert "feature_name" not in called_args[2]

    @pytest.mark.asyncio
    async def test_missing_organization_id_returns_ok_false(self, monkeypatch):
        """No organization_id argument and no session context set → clear error,
        no MCP call attempted."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            return_value=[],
        ) as mock_call:
            from plugins.tools.rag import handle

            result = await handle(query="q", workspace_id="ws-1")
        assert result["ok"] is False
        assert "organization_id is required" in result["error"]
        mock_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_resolves_org_from_workspace_not_session_context(self, monkeypatch):
        """A multi-org user's session "current" org can differ from the org
        that actually owns the workspace being queried — when organization_id
        isn't passed explicitly, the workspace's owning org must win over the
        stale session context."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        import plugins.context as ctx

        ctx.set_context("sess-rag-1", "ws-1", "", org_id="session-org")
        with (
            patch(
                "src.services.workflow_backend_client.get_workspace_organization_id",
                AsyncMock(return_value="workspace-owning-org"),
            ),
            patch(
                "plugins.tools.rag.call_mcp_tool",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_call,
        ):
            from plugins.tools.rag import handle

            result = await handle(query="q", workspace_id="ws-1")
        assert result["ok"] is True
        called_args = mock_call.await_args[0]
        assert called_args[2]["organization_id"] == "workspace-owning-org"

    @pytest.mark.asyncio
    async def test_falls_back_to_session_org_when_workspace_lookup_fails(self, monkeypatch):
        """workflow-backend being unreachable must not hard-fail the query —
        fall back to the session's org context."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        import plugins.context as ctx

        ctx.set_context("sess-rag-2", "ws-1", "", org_id="session-org")
        with (
            patch(
                "src.services.workflow_backend_client.get_workspace_organization_id",
                AsyncMock(side_effect=RuntimeError("workflow-backend unreachable")),
            ),
            patch(
                "plugins.tools.rag.call_mcp_tool",
                new_callable=AsyncMock,
                return_value=[],
            ) as mock_call,
        ):
            from plugins.tools.rag import handle

            result = await handle(query="q", workspace_id="ws-1")
        assert result["ok"] is True
        called_args = mock_call.await_args[0]
        assert called_args[2]["organization_id"] == "session-org"

    @pytest.mark.asyncio
    async def test_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        with patch(
            "plugins.tools.rag.call_mcp_tool",
            new_callable=AsyncMock,
            side_effect=TimeoutError("timeout"),
        ):
            from plugins.tools.rag import handle

            result = await handle(
                query="q", workspace_id="ws-1", organization_id="org-1"
            )
        assert result["ok"] is False
        assert "timeout" in result["error"]

    def test_check_available_false_when_unset(self):
        from plugins.tools.rag import check_available

        assert check_available() is False

    def test_check_available_true_when_set(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        from plugins.tools.rag import check_available

        assert check_available() is True

# ---------------------------------------------------------------------------
# RAG scoping — connection-scoped endpoint + explicit argument fallback
# ---------------------------------------------------------------------------


class TestRagScoping:
    @pytest.mark.asyncio
    async def test_rag_handle_resolves_workspace_from_context(self, monkeypatch):
        """query_rag resolves organization_id/workspace_id from session context
        and scopes the connection (kwargs → /ws/<org>/<ws>/sse); the explicit
        arguments are also passed so the server can cross-check the scope."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8000")

        import plugins.context as ctx

        ctx.set_context("sess-rag", "rag-workspace", "", org_id="rag-org")

        from plugins.tools.rag import handle

        with patch(
            "plugins.tools.rag.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = [{"type": "text", "text": "chunk"}]
            result = await handle(query="auth flow")

        assert result["ok"] is True
        args, kwargs = mock_call.call_args
        tool_arguments = args[2] if len(args) > 2 else {}
        assert tool_arguments.get("workspace_id") == "rag-workspace"
        assert tool_arguments.get("organization_id") == "rag-org"
        # Connection-scoped: the kwargs drive the /ws/<org>/<ws>/sse endpoint
        # selection in call_mcp_tool.
        assert kwargs.get("workspace_id") == "rag-workspace"
        assert kwargs.get("organization_id") == "rag-org"

    @pytest.mark.asyncio
    async def test_rag_handle_explicit_workspace_id_overrides_context(
        self, monkeypatch
    ):
        """Explicit workspace_id arg to rag.handle() takes precedence over context."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8000")

        import plugins.context as ctx

        ctx.set_context(
            "sess-rag2", "context-workspace", "", org_id="context-org"
        )

        from plugins.tools.rag import handle

        with patch(
            "plugins.tools.rag.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = []
            await handle(query="something", workspace_id="explicit-workspace")

        args, kwargs = mock_call.call_args
        tool_arguments = args[2] if len(args) > 2 else {}
        assert tool_arguments.get("workspace_id") == "explicit-workspace"
        assert kwargs.get("workspace_id") == "explicit-workspace"

    @pytest.mark.asyncio
    async def test_rag_handle_no_organization_context_returns_error(
        self, monkeypatch
    ):
        """Without an organization_id in session context, rag.handle() returns
        a clear error instead of querying an unscoped/wrong collection."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8000")

        import plugins.context as ctx

        ctx.set_context("sess-rag-noorg", "some-workspace", "", org_id="")

        from plugins.tools.rag import handle

        with patch(
            "plugins.tools.rag.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            result = await handle(query="auth flow")

        assert result["ok"] is False
        assert "organization_id is required" in result["error"]
        mock_call.assert_not_called()

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
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        from plugins.tools.gitnexus import check_available

        assert check_available() is True

    def test_rag_included_when_url_set(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        from plugins.tools.rag import check_available

        assert check_available() is True

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

    def test_task_summary_block_injected(self):
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

    def test_blocked_tasks_block_included_when_blocked(self):
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

    def test_blocked_tasks_absent_when_none_blocked(self):
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
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        with patch("plugins.hooks.check_workflow_available", return_value=False):
            content = self._call_inject(feature_id="")
        assert "query_gitnexus" in content

    def test_gitnexus_not_advertised_when_url_unset(self):
        with patch("plugins.hooks.check_workflow_available", return_value=False):
            content = self._call_inject(feature_id="")
        assert "query_gitnexus" not in content

    def test_rag_advertised_when_url_set(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        with patch("plugins.hooks.check_workflow_available", return_value=False):
            content = self._call_inject(feature_id="")
        assert "query_rag" in content

    def test_rag_not_advertised_when_url_unset(self):
        with patch("plugins.hooks.check_workflow_available", return_value=False):
            content = self._call_inject(feature_id="")
        assert "query_rag" not in content

    def test_task_summary_lists_all_tasks(self):
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
# inject_context() — passes workspace_id to list_indexed_repos
# ---------------------------------------------------------------------------


class TestInjectContextGitnexusScoping:
    def _set_context(self, workspace_id: str, feature_id: str = ""):
        import plugins.context as ctx

        ctx.set_context("sess-hook", workspace_id, feature_id)

    def test_inject_context_passes_workspace_id_to_list_indexed_repos(
        self, monkeypatch
    ):
        """inject_context() scopes the list_indexed_repos call to the session workspace."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        self._set_context("hook-workspace")

        captured_workspace_ids = []

        def fake_list_indexed_repos(use_cache=True, timeout=None, workspace_id=""):
            captured_workspace_ids.append(workspace_id)
            return ["repo-1", "repo-2"]

        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch(
                "plugins.tools.gitnexus.list_indexed_repos",
                side_effect=fake_list_indexed_repos,
            ),
            patch(
                "plugins.tools.workspace.handle",
                return_value={"ok": True, "workspace": {"repos": []}},
            ),
            patch("plugins.tools.feature.handle", return_value={"ok": False}),
            patch("plugins.tools.tasks.handle", return_value={"ok": True, "tasks": []}),
            patch("plugins.hooks._build_skills_block", return_value=None),
        ):
            from plugins.hooks import inject_context

            inject_context(session_id="sess-hook")

        assert len(captured_workspace_ids) == 1
        assert captured_workspace_ids[0] == "hook-workspace"

    def test_workspace_a_repos_not_leaked_to_workspace_b_injection(self, monkeypatch):
        """G2: injecting context for workspace B must not surface workspace A's repos."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        repos_by_workspace = {
            "workspace-a": ["repo-only-in-a"],
            "workspace-b": ["repo-only-in-b"],
        }

        def fake_list_indexed_repos(use_cache=True, timeout=None, workspace_id=""):
            return repos_by_workspace.get(workspace_id, [])

        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch(
                "plugins.tools.gitnexus.list_indexed_repos",
                side_effect=fake_list_indexed_repos,
            ),
            patch(
                "plugins.tools.workspace.handle",
                return_value={"ok": True, "workspace": {"repos": []}},
            ),
            patch("plugins.tools.feature.handle", return_value={"ok": False}),
            patch("plugins.tools.tasks.handle", return_value={"ok": True, "tasks": []}),
            patch("plugins.hooks._build_skills_block", return_value=None),
        ):
            from plugins.hooks import inject_context
            import plugins.context as ctx

            ctx.set_context("sess-b", "workspace-b", "")
            result_b = inject_context(session_id="sess-b")

        context_text = result_b["context"]
        assert "repo-only-in-b" in context_text
        assert "repo-only-in-a" not in context_text

    def test_inject_context_no_workspace_skips_gitnexus(self, monkeypatch):
        """When no workspace is set for a session, inject_context returns None."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.context as ctx

        ctx.clear_context("sess-none")
        ctx._local.workspace_id = ""

        with (
            patch("plugins.hooks.check_workflow_available", return_value=False),
            patch("plugins.hooks._build_skills_block", return_value=None),
        ):
            from plugins.hooks import inject_context

            result = inject_context(session_id="sess-none")

        assert result is None

# ---------------------------------------------------------------------------
# write_product_spec — content coercion (regression for
# "'dict' object has no attribute 'encode'" over the MCP path)
# ---------------------------------------------------------------------------


class TestMcpArgCoercionAndErrors:
    def test_coerce_text_unwraps_dict_query(self):
        from plugins.clients.mcp_client import coerce_text

        assert coerce_text("auth flow") == "auth flow"
        assert coerce_text({"query": "auth flow"}) == "auth flow"
        assert coerce_text({"q": "x"}) == "x"
        assert coerce_text(None) == ""

    def test_unwrap_exception_drills_into_group(self):
        from plugins.clients.mcp_client import _unwrap_exception

        eg = ExceptionGroup("tg", [ConnectionError("connection refused")])
        leaf = _unwrap_exception(eg)
        assert isinstance(leaf, ConnectionError)
        assert str(leaf) == "connection refused"

    @pytest.mark.asyncio
    async def test_rag_coerces_dict_query_to_string(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8003")
        with patch(
            "plugins.tools.rag.call_mcp_tool", new_callable=AsyncMock, return_value=[]
        ) as mock_call:
            from plugins.tools.rag import handle

            # Model passes query as a structured object (the blocker case).
            result = await handle(
                query={"query": "auth flow"},
                workspace_id="ws-1",
                organization_id="org-1",
            )
        assert result["ok"] is True
        # The forwarded argument must be a plain string, not a dict.
        assert mock_call.await_args[0][2]["query"] == "auth flow"

    @pytest.mark.asyncio
    async def test_gitnexus_coerces_dict_query(self, monkeypatch):
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        import plugins.context as ctx

        ctx.set_context("sess-coerce", "test-workspace", "", org_id="test-org")
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
        from plugins.clients import mcp_client

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
        captured = {}

        def fake_write(workspace_id, feature_id, path, content, **_kw):
            captured["content"] = content
            return {"ok": True, "version_id": "v1"}

        with (
            patch("plugins.tools.artifacts.write_document_content", side_effect=fake_write),
            patch("plugins.context.was_context_gathered", return_value=True),
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

# ---------------------------------------------------------------------------
# SkillEntry dataclass
# ---------------------------------------------------------------------------


class TestSkillEntry:
    def test_fields_accessible(self):
        from plugins.skills.index import SkillEntry

        e = SkillEntry(name="foo", description="A foo skill", path="claude/technical_skills/foo")
        assert e.name == "foo"
        assert e.description == "A foo skill"
        assert e.path == "claude/technical_skills/foo"
        assert e.is_authoring is False
        assert e.body == ""
        assert e.references == {}

    def test_body_setter(self):
        from plugins.skills.index import SkillEntry

        e = SkillEntry(name="foo", description="desc", path="some/path")
        e.body = "# Foo\nContent here."
        assert e.body == "# Foo\nContent here."

    def test_authoring_flag(self):
        from plugins.skills.index import SkillEntry

        e = SkillEntry(name="tech-lead", description="Tech lead authoring skill",
                       path="claude/workflow_skills/tech-lead", is_authoring=True)
        assert e.is_authoring is True

# ---------------------------------------------------------------------------
# _parse_description
# ---------------------------------------------------------------------------


class TestParseDescription:
    def test_extracts_description_from_frontmatter(self):
        from plugins.skills.index import _parse_description

        skill_md = "---\nname: foo\ndescription: A helpful skill for doing things.\n---\n# Foo"
        assert _parse_description(skill_md) == "A helpful skill for doing things."

    def test_returns_empty_when_no_frontmatter(self):
        from plugins.skills.index import _parse_description

        assert _parse_description("# No frontmatter here") == ""

    def test_returns_empty_when_no_description_field(self):
        from plugins.skills.index import _parse_description

        skill_md = "---\nname: foo\n---\n# Content"
        assert _parse_description(skill_md) == ""

    def test_handles_multiline_frontmatter(self):
        from plugins.skills.index import _parse_description

        skill_md = "---\nname: foo\nother: value\ndescription: This is the description.\n---\n"
        assert _parse_description(skill_md) == "This is the description."

# ---------------------------------------------------------------------------
# build_index / get_index — loads the real bundle, then caches
# ---------------------------------------------------------------------------


class TestBuildIndex:
    def test_loads_bundled_skills(self):
        from plugins.skills.index import build_index

        index = build_index()
        # Known knowledge skills ship in the bundle.
        assert "python-best-practices" in index
        assert "typescript-best-practices" in index
        assert index["python-best-practices"].is_authoring is False

    def test_no_workflow_skills_bundled(self):
        from plugins.skills.index import build_index

        index = build_index()
        assert not any(e.is_authoring for e in index.values())

    def test_empty_when_bundle_missing(self, monkeypatch, tmp_path):
        import plugins.skills.index as index_mod

        monkeypatch.setattr(index_mod, "_BUNDLE_ROOT", tmp_path / "does-not-exist")
        assert index_mod.build_index() == {}


class TestGetIndex:
    def test_result_is_cached_and_built_once(self):
        with patch(
            "plugins.skills.index._build_index_from_bundle",
            return_value={"python-best-practices": MagicMock()},
        ) as mock_build:
            from plugins.skills import get_index

            r1 = get_index()
            r2 = get_index()

        assert r1 is r2
        mock_build.assert_called_once()

    def test_get_skill_returns_none_for_unknown(self):
        from plugins.skills import get_skill

        assert get_skill("no-such-skill-xyz") is None

    def test_get_skill_returns_entry_when_known(self):
        from plugins.skills import get_skill

        entry = get_skill("python-best-practices")
        assert entry is not None
        assert entry.name == "python-best-practices"
        assert entry.description

# ---------------------------------------------------------------------------
# get_shared_rules — CLAUDE.shared.md (injected into the system prompt)
# ---------------------------------------------------------------------------


class TestSharedRules:
    def test_returns_bundled_shared_rules(self):
        from plugins.skills import get_shared_rules

        rules = get_shared_rules()
        assert rules  # non-empty
        assert "Feature lifecycle" in rules

    def test_cached_after_first_read(self):
        import plugins.skills.index as index_mod

        with patch.object(index_mod, "_read", wraps=index_mod._read) as spy:
            index_mod.get_shared_rules()
            index_mod.get_shared_rules()
        assert spy.call_count == 1

    def test_empty_when_file_missing(self, monkeypatch, tmp_path):
        import plugins.skills.index as index_mod

        monkeypatch.setattr(index_mod, "_BUNDLE_ROOT", tmp_path / "missing")
        assert index_mod.get_shared_rules() == ""

# ---------------------------------------------------------------------------
# Bucket assignment — technical = knowledge, workflow = authoring
# ---------------------------------------------------------------------------


class TestSkillBuckets:
    def test_technical_skills_are_knowledge(self):
        from plugins.skills.index import build_index

        index = build_index()
        assert index["python-best-practices"].is_authoring is False
        assert index["typescript-best-practices"].is_authoring is False

    def test_entry_path_reflects_bucket(self):
        from plugins.skills import get_skill

        assert get_skill("python-best-practices").path.startswith("technical_skills/")

# ---------------------------------------------------------------------------
# _build_index_from_bundle / _index_skill — directory walking
# ---------------------------------------------------------------------------


class TestBuildIndexFromBundle:
    def test_indexes_technical_skills(self, tmp_path):
        _write_skill(tmp_path / "technical_skills" / "python-best-practices", "Python best practices.")

        from plugins.skills.index import _build_index_from_bundle

        index = _build_index_from_bundle(tmp_path)

        assert index["python-best-practices"].description == "Python best practices."
        assert index["python-best-practices"].is_authoring is False

    def test_skips_skill_without_description(self, tmp_path):
        _write_skill(tmp_path / "technical_skills" / "no-desc", None)

        from plugins.skills.index import _build_index_from_bundle

        assert "no-desc" not in _build_index_from_bundle(tmp_path)

    def test_collects_reference_files(self, tmp_path):
        ts_dir = tmp_path / "technical_skills" / "ts"
        _write_skill(ts_dir, "TypeScript skill.")
        (ts_dir / "advanced-types.md").write_text("# Advanced Types", encoding="utf-8")
        (ts_dir / "references").mkdir()
        (ts_dir / "references" / "patterns.md").write_text("# Patterns", encoding="utf-8")

        from plugins.skills.index import _build_index_from_bundle

        entry = _build_index_from_bundle(tmp_path)["ts"]
        assert entry.references["advanced-types.md"] == "# Advanced Types"
        assert entry.references["references/patterns.md"] == "# Patterns"

# ---------------------------------------------------------------------------
# load_skill tool handler
# ---------------------------------------------------------------------------


class TestLoadSkillHandler:
    def _make_entry(self, name: str = "python-best-practices") -> object:
        from plugins.skills.index import SkillEntry

        e = SkillEntry(name=name, description=f"{name} description", path=f"p/{name}")
        e.body = f"# {name}\nContent."
        e.references = {"ref.md": "# Reference"}
        return e

    def test_happy_path_returns_skill(self):
        entry = self._make_entry()
        with patch("plugins.skills.get_skill", return_value=entry):
            from plugins.tools.skills import handle

            result = handle(name="python-best-practices")

        assert result["ok"] is True
        skill = result["skill"]
        assert skill["name"] == "python-best-practices"
        assert skill["description"] == "python-best-practices description"
        assert "# python-best-practices" in skill["body"]
        assert "ref.md" in skill["references"]

    def test_unknown_skill_returns_error(self):
        with patch("plugins.skills.get_skill", return_value=None):
            with patch("plugins.skills.get_index", return_value={"python-best-practices": MagicMock()}):
                from plugins.tools.skills import handle

                result = handle(name="no-such-skill")

        assert result["ok"] is False
        assert "no-such-skill" in result["error"]
        assert "python-best-practices" in result["error"]

    def test_empty_index_returns_descriptive_error(self):
        with (
            patch("plugins.skills.get_skill", return_value=None),
            patch("plugins.skills.get_index", return_value={}),
        ):
            from plugins.tools.skills import handle

            result = handle(name="something")

        assert result["ok"] is False
        assert "plugins/skills" in result["error"]

    def test_empty_name_returns_error(self):
        from plugins.tools.skills import handle

        result = handle(name="")
        assert result["ok"] is False
        assert "non-empty" in result["error"]

    def test_dict_name_is_coerced(self):
        """Over the MCP path the model may pass {"name": "..."} instead of a str.
        It must not raise 'dict' object has no attribute 'strip'."""
        entry = self._make_entry()
        with patch("plugins.skills.get_skill", return_value=entry) as mock_get:
            from plugins.tools.skills import handle

            result = handle(name={"name": "python-best-practices"})
        assert result["ok"] is True
        mock_get.assert_called_once_with("python-best-practices")

    def test_non_string_name_does_not_raise(self):
        from plugins.tools.skills import handle

        # A bare None / number must degrade to the empty-name error, not crash.
        assert handle(name=None)["ok"] is False
        assert handle(name=123)["ok"] is False

    def test_whitespace_stripped(self):
        entry = self._make_entry()
        with patch("plugins.skills.get_skill", return_value=entry) as mock_get:
            from plugins.tools.skills import handle

            handle(name="  python-best-practices  ")
        mock_get.assert_called_once_with("python-best-practices")

    def test_references_returned_as_copy(self):
        entry = self._make_entry()
        with patch("plugins.skills.get_skill", return_value=entry):
            from plugins.tools.skills import handle

            r1 = handle(name="python-best-practices")
            r2 = handle(name="python-best-practices")

        r1["skill"]["references"]["new_key"] = "injected"
        assert "new_key" not in r2["skill"]["references"]


class TestLoadSkillCheckAvailable:
    def test_true_when_bundle_present(self):
        from plugins.tools.skills import check_available

        # The real bundle ships with the repo, so the index is non-empty.
        assert check_available() is True

    def test_false_when_index_empty(self):
        with patch("plugins.skills.get_index", return_value={}):
            from plugins.tools.skills import check_available

            assert check_available() is False

# ---------------------------------------------------------------------------
# _TOOLS registration
# ---------------------------------------------------------------------------


class TestToolsRegistration:
    def test_load_skill_in_tools_list(self):
        from plugins import _TOOLS

        names = [t["name"] for t in _TOOLS]
        assert "load_skill" in names

    def test_load_skill_has_required_fields(self):
        from plugins import _TOOLS

        tool = next(t for t in _TOOLS if t["name"] == "load_skill")
        assert "schema" in tool
        assert "handler" in tool
        assert "check_fn" in tool

    def test_register_includes_load_skill(self):
        ctx = MagicMock()
        from plugins import register

        register(ctx)
        registered_names = [call.kwargs.get("name") or call.args[0]
                            for call in ctx.register_tool.call_args_list]
        assert "load_skill" in registered_names

# ---------------------------------------------------------------------------
# _skills_for_repos stack matching
# ---------------------------------------------------------------------------


class TestSkillsForRepos:
    def test_ui_repo_returns_frontend_skills(self):
        from plugins.hooks import _skills_for_repos

        skills = _skills_for_repos(["digital-factory-ui"])
        assert "typescript-best-practices" in skills
        assert "frontend-engineer" in skills

    def test_hermes_repo_returns_python_skills(self):
        from plugins.hooks import _skills_for_repos

        skills = _skills_for_repos(["hermes-agent"])
        assert "python-best-practices" in skills

    def test_workflow_repo_returns_go_skills(self):
        from plugins.hooks import _skills_for_repos

        skills = _skills_for_repos(["workflow-backend"])
        assert "go-best-practices" in skills

    def test_multiple_repos_deduped(self):
        from plugins.hooks import _skills_for_repos

        skills = _skills_for_repos(["hermes-agent", "hermes-agent"])
        assert skills.count("python-best-practices") == 1

    def test_unknown_repo_returns_empty(self):
        from plugins.hooks import _skills_for_repos

        skills = _skills_for_repos(["totally-unknown-repo-xyz"])
        assert skills == []

# ---------------------------------------------------------------------------
# inject_context — skills injection
# ---------------------------------------------------------------------------


class TestInjectContextSkills:
    def _call_inject(self, workspace_id: str = "ws-1", feature_id: str = "feat-1"):
        import plugins.context as ctx_mod
        ctx_mod.set_context("sess-1", workspace_id, feature_id)
        from plugins.hooks import inject_context

        result = inject_context(session_id="sess-1")
        return result["context"] if result else ""

    def test_no_skills_block_when_index_empty(self):
        with (
            patch("plugins.hooks._build_skills_block", return_value=None),
            patch("plugins.hooks.check_workflow_available", return_value=False),
            patch("plugins.hooks.get_index", return_value={}),
        ):
            content = self._call_inject()
        assert "Available skills" not in content

    def test_skills_block_injected_when_index_populated(self):
        skills_block = "## Available skills (call load_skill to load full content)\n  python-best-practices: Python skill"
        with (
            patch("plugins.hooks._build_skills_block", return_value=skills_block),
            patch("plugins.hooks.check_workflow_available", return_value=False),
        ):
            content = self._call_inject()
        assert "Available skills" in content
        assert "python-best-practices" in content

    def test_load_skill_advertised_when_index_populated(self):
        with (
            patch("plugins.hooks._build_skills_block", return_value=None),
            patch("plugins.hooks.check_workflow_available", return_value=False),
            patch("plugins.hooks.get_index", return_value={"python-best-practices": MagicMock()}),
        ):
            content = self._call_inject()
        assert "load_skill" in content

    def test_load_skill_not_advertised_when_index_empty(self):
        with (
            patch("plugins.hooks._build_skills_block", return_value=None),
            patch("plugins.hooks.check_workflow_available", return_value=False),
            patch("plugins.hooks.get_index", return_value={}),
        ):
            content = self._call_inject()
        assert "load_skill" not in content

    def test_skills_block_called_with_stage(self, monkeypatch):
        fake_feature = {
            "ok": True,
            "feature": {
                "title": "Test Feature",
                "feature_name": "test-feature",
                "stage": "technical_design",
                "status": "in_implementation",
                "next_action": None,
            },
        }
        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch("plugins.tools.workspace.handle", return_value={"ok": True, "workspace": {"repos": []}}),
            patch("plugins.tools.feature.handle", return_value=fake_feature),
            patch("plugins.tools.tasks.handle", return_value={"ok": True, "tasks": []}),
            patch("plugins.hooks._build_skills_block", return_value=None) as mock_block,
        ):
            self._call_inject()
        mock_block.assert_called_once()
        _, stage, _ = mock_block.call_args[0]
        assert stage == "technical_design"


class TestBuildSkillsBlock:
    def _make_entry(self, name: str, desc: str, is_authoring: bool = False):
        from plugins.skills.index import SkillEntry

        e = SkillEntry(name=name, description=desc, path="p", is_authoring=is_authoring)
        e.body = "# Body"
        return e

    def test_returns_none_when_index_empty(self):
        with patch("plugins.hooks.get_index", return_value={}):
            from plugins.hooks import _build_skills_block

            assert _build_skills_block("feat-1", "technical_design", ["hermes-agent"]) is None

    def test_knowledge_skills_listed(self):
        index = {
            "python-best-practices": self._make_entry("python-best-practices", "Python skill"),
            "tech-lead": self._make_entry("tech-lead", "Authoring skill", is_authoring=True),
        }
        with patch("plugins.hooks.get_index", return_value=index):
            from plugins.hooks import _build_skills_block

            block = _build_skills_block("feat-1", "in_implementation", [])

        assert "python-best-practices" in block
        assert "Knowledge skills" in block

    def test_authoring_skills_listed(self):
        index = {
            "tech-lead": self._make_entry("tech-lead", "Tech lead skill", is_authoring=True),
        }
        with patch("plugins.hooks.get_index", return_value=index):
            from plugins.hooks import _build_skills_block

            block = _build_skills_block("feat-1", "in_implementation", [])

        assert "tech-lead" in block
        assert "Authoring skills" in block

    def test_technical_design_stage_surfaces_stack_matched_first(self):
        index = {
            "python-best-practices": self._make_entry("python-best-practices", "Python skill"),
            "go-best-practices": self._make_entry("go-best-practices", "Go skill"),
            "typescript-best-practices": self._make_entry("typescript-best-practices", "TS skill"),
        }
        with (
            patch("plugins.hooks.get_index", return_value=index),
            patch("plugins.hooks._skills_for_repos", return_value=["python-best-practices"]),
        ):
            from plugins.hooks import _build_skills_block

            block = _build_skills_block("feat-1", "technical_design", ["hermes-agent"])

        assert "Stack-matched skills" in block
        stack_pos = block.find("Stack-matched")
        other_pos = block.find("Other knowledge")
        python_pos = block.find("python-best-practices")
        assert python_pos > stack_pos
        assert python_pos < other_pos

    def test_non_technical_design_stage_no_stack_matching(self):
        index = {
            "python-best-practices": self._make_entry("python-best-practices", "Python skill"),
        }
        with patch("plugins.hooks.get_index", return_value=index):
            from plugins.hooks import _build_skills_block

            block = _build_skills_block("feat-1", "in_implementation", ["hermes-agent"])

        assert "Stack-matched" not in block
