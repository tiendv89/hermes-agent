"""Tests for T7 — hermes-agent connection-scoped endpoints + GitNexus workspace scoping.

Covers:
  - _sse_endpoint: workspace-scoped URL construction (with and without workspace_id)
  - call_mcp_tool: passes workspace_id to _sse_endpoint
  - gitnexus.handle(): resolves workspace_id from session context, scopes SSE connection
  - list_indexed_repos(): accepts workspace_id, partitions cache by workspace
  - inject_context(): passes workspace_id when calling list_indexed_repos (G2 isolation)
  - RAG scoping: rag.handle() still resolves workspace from context (unchanged)
  - Isolation: a session bound to workspace A never reaches workspace B's endpoint
"""

from __future__ import annotations

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
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
    yield


# ---------------------------------------------------------------------------
# _sse_endpoint — URL construction
# ---------------------------------------------------------------------------


class TestSseEndpoint:
    def test_bare_host_no_workspace_leaves_path_unchanged(self):
        from plugins.mcp_client import _sse_endpoint

        assert _sse_endpoint("https://rag.example.com") == "https://rag.example.com"

    def test_bare_host_with_workspace_scopes_to_ws_path(self):
        from plugins.mcp_client import _sse_endpoint

        result = _sse_endpoint("https://rag.example.com", workspace_id="my-ws")
        assert result == "https://rag.example.com/ws/my-ws/sse"

    def test_root_path_no_workspace_leaves_path_unchanged(self):
        from plugins.mcp_client import _sse_endpoint

        assert _sse_endpoint("http://gitnexus:8002/") == "http://gitnexus:8002/"

    def test_root_path_with_workspace_uses_ws_path(self):
        from plugins.mcp_client import _sse_endpoint

        result = _sse_endpoint(
            "http://gitnexus:8002/", workspace_id="project-workspace"
        )
        assert result == "http://gitnexus:8002/ws/project-workspace/sse"

    def test_explicit_path_no_workspace_passed_through_verbatim(self):
        from plugins.mcp_client import _sse_endpoint

        # Without a workspace_id the configured URL is used verbatim — the
        # helper never invents a path.
        assert _sse_endpoint("http://host:8002/custom") == "http://host:8002/custom"

    def test_explicit_path_with_workspace_replaced(self):
        from plugins.mcp_client import _sse_endpoint

        # When workspace_id is given, always use the scoped path — a path left
        # in the env var must not bypass workspace scoping.
        result = _sse_endpoint("http://gitnexus:8002/custom", workspace_id="faro")
        assert result == "http://gitnexus:8002/ws/faro/sse"

    def test_empty_workspace_id_leaves_path_unchanged(self):
        from plugins.mcp_client import _sse_endpoint

        assert (
            _sse_endpoint("https://rag.example.com", workspace_id="")
            == "https://rag.example.com"
        )

    def test_different_workspaces_produce_different_urls(self):
        from plugins.mcp_client import _sse_endpoint

        url_a = _sse_endpoint("http://gitnexus:8002", workspace_id="workspace-a")
        url_b = _sse_endpoint("http://gitnexus:8002", workspace_id="workspace-b")
        assert url_a != url_b
        assert "workspace-a" in url_a
        assert "workspace-b" in url_b

    def test_workspace_id_preserves_query_params(self):
        from plugins.mcp_client import _sse_endpoint

        result = _sse_endpoint("http://host:8000?token=abc", workspace_id="ws1")
        assert "/ws/ws1/sse" in result
        assert "token=abc" in result


# ---------------------------------------------------------------------------
# call_mcp_tool — passes workspace_id through
# ---------------------------------------------------------------------------


class TestCallMcpToolWorkspaceId:
    @pytest.mark.asyncio
    async def test_no_workspace_id_passes_empty_string_to_sse_endpoint(self):
        from plugins.mcp_client import call_mcp_tool

        with (
            patch(
                "plugins.mcp_client._sse_endpoint", return_value="http://host"
            ) as mock_ep,
            patch("plugins.mcp_client.sse_client") as mock_sse,
        ):
            mock_session = AsyncMock()
            mock_session.initialize = AsyncMock()
            mock_session.call_tool = AsyncMock(return_value=MagicMock(content=[]))
            mock_sse.return_value.__aenter__ = AsyncMock(
                return_value=(AsyncMock(), AsyncMock())
            )
            mock_sse.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("plugins.mcp_client.ClientSession") as mock_cs:
                mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
                await call_mcp_tool("http://host", "rag_query", {"query": "test"})

        mock_ep.assert_called_once_with("http://host", "")

    @pytest.mark.asyncio
    async def test_workspace_id_forwarded_to_sse_endpoint(self):
        from plugins.mcp_client import call_mcp_tool

        with (
            patch(
                "plugins.mcp_client._sse_endpoint",
                return_value="http://host/ws/ws1/sse",
            ) as mock_ep,
            patch("plugins.mcp_client.sse_client") as mock_sse,
        ):
            mock_session = AsyncMock()
            mock_session.initialize = AsyncMock()
            mock_session.call_tool = AsyncMock(return_value=MagicMock(content=[]))
            mock_sse.return_value.__aenter__ = AsyncMock(
                return_value=(AsyncMock(), AsyncMock())
            )
            mock_sse.return_value.__aexit__ = AsyncMock(return_value=False)

            with patch("plugins.mcp_client.ClientSession") as mock_cs:
                mock_cs.return_value.__aenter__ = AsyncMock(return_value=mock_session)
                mock_cs.return_value.__aexit__ = AsyncMock(return_value=False)
                await call_mcp_tool("http://host", "list_repos", {}, workspace_id="ws1")

        mock_ep.assert_called_once_with("http://host", "ws1")


# ---------------------------------------------------------------------------
# gitnexus.handle() — resolves workspace from context
# ---------------------------------------------------------------------------


class TestGitnexusHandleWorkspaceScoping:
    @pytest.mark.asyncio
    async def test_handle_resolves_workspace_from_context(self, monkeypatch):
        """handle() reads workspace_id from session context and passes it to call_mcp_tool."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")

        import plugins.context as ctx

        ctx.set_context("sess-a", "workspace-a", "feat-1")

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

        ctx.set_context("sess-a", "workspace-a", "feat-1")

        from plugins.tools.gitnexus import handle

        captured_workspace_ids = []

        async def fake_call(url, tool, args, workspace_id=""):
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

        ctx.set_context("sess-x", "my-workspace", "")

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

            result = list_indexed_repos(workspace_id="ws-alpha")

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

        async def fake_call(url, tool, args, workspace_id=""):
            call_count["n"] += 1
            return responses.get(workspace_id, [])

        with patch("plugins.tools.gitnexus.call_mcp_tool", side_effect=fake_call):
            from plugins.tools.gitnexus import list_indexed_repos

            repos_a = list_indexed_repos(workspace_id="ws-a")
            repos_b = list_indexed_repos(workspace_id="ws-b")

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

            list_indexed_repos(workspace_id="cached-ws")
            list_indexed_repos(workspace_id="cached-ws")

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

            list_indexed_repos(workspace_id="ws-1")
            list_indexed_repos(workspace_id="ws-2")

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


class TestResolveWorkspaceSlug:
    # Direct unit coverage of resolve_workspace_slug itself (empty/no-config
    # passthrough, lookup-miss/error fallback) now lives in
    # tests/src/test_workflow_backend_client.py::TestResolveWorkspaceSlug —
    # this class covers gitnexus/rag callers threading the resolved slug
    # through correctly.

    @pytest.mark.asyncio
    async def test_gitnexus_handle_resolves_uuid_to_slug_before_scoping(
        self, monkeypatch
    ):
        """handle() scopes the SSE connection to the resolved slug, not the raw UUID."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        import plugins.context as ctx

        ctx.set_context("sess-uuid", "11111111-1111-1111-1111-111111111111", "")

        from plugins.tools.gitnexus import handle

        with (
            patch(
                "src.services.workflow_backend_client.get_workspace_slug",
                AsyncMock(return_value="voyager-interface"),
            ),
            patch(
                "plugins.tools.gitnexus.call_mcp_tool", new_callable=AsyncMock
            ) as mock_call,
        ):
            mock_call.return_value = []
            await handle(query="Symbol", tool="query")

        _, kwargs = mock_call.call_args
        assert kwargs.get("workspace_id") == "voyager-interface"

    def test_list_indexed_repos_caches_by_resolved_slug(self, monkeypatch):
        """A UUID and its slug alias share one cache entry, keyed by the slug."""
        monkeypatch.setenv("GITNEXUS_MCP_URL", "http://gitnexus:8002")
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        import plugins.tools.gitnexus as gn_mod

        gn_mod._repo_cache.clear()

        with (
            patch(
                "src.services.workflow_backend_client.get_workspace_slug",
                AsyncMock(return_value="aliased-workspace"),
            ),
            patch(
                "plugins.tools.gitnexus.call_mcp_tool", new_callable=AsyncMock
            ) as mock_call,
        ):
            mock_call.return_value = [{"type": "text", "text": '[{"name":"repo-a"}]'}]
            from plugins.tools.gitnexus import list_indexed_repos

            list_indexed_repos(workspace_id="11111111-1111-1111-1111-111111111111")
            list_indexed_repos(workspace_id="aliased-workspace")

        assert mock_call.call_count == 1
        assert set(gn_mod._repo_cache.keys()) == {"aliased-workspace"}

    @pytest.mark.asyncio
    async def test_rag_handle_resolves_uuid_to_slug(self, monkeypatch):
        """rag.handle() also resolves a UUID workspace_id to its canonical slug
        before forwarding it as the rag_query filter argument."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8000")
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        import plugins.context as ctx

        ctx.set_context("sess-rag-uuid", "22222222-2222-2222-2222-222222222222", "")

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
        assert tool_arguments.get("workspace_id") == "rag-workspace-slug"

    @pytest.mark.asyncio
    async def test_rag_handle_no_workflow_db_passes_raw_value(self, monkeypatch):
        """Without WORKFLOW_DATABASE_URL, rag.handle() still forwards the raw value
        (no regression for deployments without the workflow DB configured)."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8000")

        import plugins.context as ctx

        ctx.set_context("sess-rag-raw", "raw-workspace", "")

        from plugins.tools.rag import handle

        with patch(
            "plugins.tools.rag.call_mcp_tool", new_callable=AsyncMock
        ) as mock_call:
            mock_call.return_value = []
            await handle(query="auth flow")

        args, _ = mock_call.call_args
        tool_arguments = args[2] if len(args) > 2 else {}
        assert tool_arguments.get("workspace_id") == "raw-workspace"


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
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
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
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")

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
# RAG scoping — connection-scoped endpoint + explicit argument fallback
# ---------------------------------------------------------------------------


class TestRagScoping:
    @pytest.mark.asyncio
    async def test_rag_handle_resolves_workspace_from_context(self, monkeypatch):
        """query_rag resolves workspace_id from session context and scopes the
        connection (workspace_id kwarg → /ws/<slug>/sse); the explicit argument
        is also passed so the server can cross-check the scope."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8000")

        import plugins.context as ctx

        ctx.set_context("sess-rag", "rag-workspace", "")

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
        # Connection-scoped: the workspace_id kwarg drives the /ws/<slug>/sse
        # endpoint selection in call_mcp_tool.
        assert kwargs.get("workspace_id") == "rag-workspace"

    @pytest.mark.asyncio
    async def test_rag_handle_explicit_workspace_id_overrides_context(
        self, monkeypatch
    ):
        """Explicit workspace_id arg to rag.handle() takes precedence over context."""
        monkeypatch.setenv("RAG_MCP_URL", "http://rag:8000")

        import plugins.context as ctx

        ctx.set_context("sess-rag2", "context-workspace", "")

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
