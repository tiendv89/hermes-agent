"""Tests for plugins.tools.workspace (get_workspace_context tool).

Covers the storage-service/RAG augmentation on top of the workflow-backend
repo lookup:
  - workspace-root documents are listed via storage-service and attached
  - a workspace-root CLAUDE.md/README.md/overview.md/summary.md doc, if
    present, is read and used as the summary (no RAG call)
  - absent a summary doc, a best-effort RAG query supplies the summary
  - RAG unavailable and no summary doc -> empty summary, no crash
  - missing workspace_id / lookup failure -> {"ok": False, ...}
"""

from __future__ import annotations

import importlib
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_WID = "ws-1"


def _storage_service_error_cls():
    # Same staleness hazard as _workspace_tool() below: test_approval.py's
    # sys.modules purge means a module-level import of StorageServiceError
    # can be a different (pre-purge) class object than the one workspace.py
    # actually catches after being freshly re-imported, so `except
    # StorageServiceError` silently fails to match. Resolve fresh each time.
    return importlib.import_module("plugins.clients.storage_service_client").StorageServiceError


def _workspace_tool():
    # Some other test files (e.g. test_approval.py) purge every
    # "plugins.*"/"src.*" entry from sys.modules around their own tests. A
    # module-level `from plugins.tools import workspace as workspace_tool`
    # captured here would go stale the moment that happens: patch("plugins.
    # tools.workspace.X", ...) re-imports and patches a brand new module
    # object, while our stale reference keeps calling the untouched original.
    # Re-resolve fresh, after patches are already active, so we always get
    # whatever module object patch() itself resolved against.
    return importlib.import_module("plugins.tools.workspace")


def _ctx_stack(stack: ExitStack, user_id="u-1", org_id="org-1", workspace_id=_WID) -> None:
    stack.enter_context(patch("plugins.context.get_user_id", return_value=user_id))
    stack.enter_context(patch("plugins.context.get_org_id", return_value=org_id))
    stack.enter_context(patch("plugins.context.get_workspace_id", return_value=workspace_id))


class TestGetWorkspaceContextDocumentsAndSummary:
    def test_no_documents_no_rag_url_yields_empty_summary(self, monkeypatch):
        monkeypatch.delenv("RAG_MCP_URL", raising=False)
        with ExitStack() as stack:
            _ctx_stack(stack)
            stack.enter_context(patch(
                "plugins.tools.workspace.get_workspace_context",
                AsyncMock(return_value={"management_repo": "r1", "repos": []}),
            ))
            stack.enter_context(patch("plugins.tools.workspace.run_async", side_effect=lambda coro: _run(coro)))
            stack.enter_context(patch("plugins.tools.workspace.list_documents", return_value={"documents": []}))
            result = _workspace_tool().handle()

        assert result["ok"] is True
        assert result["workspace"]["documents"] == []
        assert result["workspace"]["summary"] == []
        assert result["workspace"]["summary_source"] is None

    def test_workspace_root_claude_md_used_as_summary_no_rag_call(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag.test")
        documents = [
            {"id": "d1", "path": "CLAUDE.md"},
            {"id": "d2", "path": "docs/features/feat-1/product_spec.md", "feature_id": "feat-1"},
        ]
        mock_rag_call = AsyncMock(return_value=[{"text": "should not be called"}])
        with ExitStack() as stack:
            _ctx_stack(stack)
            stack.enter_context(patch(
                "plugins.tools.workspace.get_workspace_context",
                AsyncMock(return_value={"management_repo": "r1", "repos": []}),
            ))
            stack.enter_context(patch("plugins.tools.workspace.run_async", side_effect=lambda coro: _run(coro)))
            stack.enter_context(patch("plugins.tools.workspace.list_documents", return_value={"documents": documents}))
            stack.enter_context(patch(
                "plugins.tools.workspace.read_document_content",
                return_value={"content": "# Workspace overview\n", "version_id": "v1"},
            ))
            stack.enter_context(patch("plugins.clients.mcp_client.call_mcp_tool", mock_rag_call))
            result = _workspace_tool().handle()

        assert result["ok"] is True
        assert result["workspace"]["summary"] == "# Workspace overview\n"
        assert result["workspace"]["summary_source"] == "CLAUDE.md"
        # Only the workspace-root doc is surfaced in "documents"; the
        # feature-owned doc is excluded.
        assert result["workspace"]["documents"] == [{"id": "d1", "path": "CLAUDE.md"}]
        mock_rag_call.assert_not_awaited()

    def test_falls_back_to_rag_when_no_summary_doc_present(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag.test")
        documents = [{"id": "d1", "path": "shared/logo.png"}]
        rag_result = [{"text": "This workspace is a checkout service."}]
        mock_rag_call = AsyncMock(return_value=rag_result)
        with ExitStack() as stack:
            _ctx_stack(stack)
            stack.enter_context(patch(
                "plugins.tools.workspace.get_workspace_context",
                AsyncMock(return_value={"management_repo": "r1", "repos": []}),
            ))
            stack.enter_context(patch("plugins.tools.workspace.run_async", side_effect=lambda coro: _run(coro)))
            stack.enter_context(patch("plugins.tools.workspace.list_documents", return_value={"documents": documents}))
            stack.enter_context(patch("plugins.clients.mcp_client.call_mcp_tool", mock_rag_call))
            result = _workspace_tool().handle()

        assert result["ok"] is True
        assert result["workspace"]["summary"] == rag_result
        assert result["workspace"]["summary_source"] == "rag"
        mock_rag_call.assert_awaited_once()

    def test_list_documents_failure_is_non_fatal(self, monkeypatch):
        monkeypatch.delenv("RAG_MCP_URL", raising=False)
        with ExitStack() as stack:
            _ctx_stack(stack)
            stack.enter_context(patch(
                "plugins.tools.workspace.get_workspace_context",
                AsyncMock(return_value={"management_repo": "r1", "repos": []}),
            ))
            stack.enter_context(patch("plugins.tools.workspace.run_async", side_effect=lambda coro: _run(coro)))
            stack.enter_context(patch(
                "plugins.tools.workspace.list_documents",
                side_effect=_storage_service_error_cls()("boom"),
            ))
            result = _workspace_tool().handle()

        assert result["ok"] is True
        assert result["workspace"]["documents"] == []
        assert result["workspace"]["summary"] == []

    def test_summary_doc_read_failure_falls_back_to_rag(self, monkeypatch):
        monkeypatch.setenv("RAG_MCP_URL", "http://rag.test")
        documents = [{"id": "d1", "path": "README.md"}]
        rag_result = [{"text": "fallback summary"}]
        mock_rag_call = AsyncMock(return_value=rag_result)
        with ExitStack() as stack:
            _ctx_stack(stack)
            stack.enter_context(patch(
                "plugins.tools.workspace.get_workspace_context",
                AsyncMock(return_value={"management_repo": "r1", "repos": []}),
            ))
            stack.enter_context(patch("plugins.tools.workspace.run_async", side_effect=lambda coro: _run(coro)))
            stack.enter_context(patch("plugins.tools.workspace.list_documents", return_value={"documents": documents}))
            stack.enter_context(patch(
                "plugins.tools.workspace.read_document_content",
                side_effect=_storage_service_error_cls()("not found"),
            ))
            stack.enter_context(patch("plugins.clients.mcp_client.call_mcp_tool", mock_rag_call))
            result = _workspace_tool().handle()

        assert result["ok"] is True
        assert result["workspace"]["summary"] == rag_result
        assert result["workspace"]["summary_source"] == "rag"

    def test_missing_workspace_id_returns_error(self):
        with ExitStack() as stack:
            stack.enter_context(patch("plugins.context.get_user_id", return_value="u-1"))
            stack.enter_context(patch("plugins.context.get_org_id", return_value="org-1"))
            stack.enter_context(patch("plugins.context.get_workspace_id", return_value=""))
            result = _workspace_tool().handle()

        assert result["ok"] is False
        assert "workspace_id" in result["error"]

    def test_get_workspace_context_failure_returns_error(self, monkeypatch):
        monkeypatch.delenv("RAG_MCP_URL", raising=False)
        with ExitStack() as stack:
            _ctx_stack(stack)
            stack.enter_context(patch(
                "plugins.tools.workspace.get_workspace_context",
                AsyncMock(side_effect=RuntimeError("workflow-backend unreachable")),
            ))
            stack.enter_context(patch("plugins.tools.workspace.run_async", side_effect=lambda coro: _run(coro)))
            result = _workspace_tool().handle()

        assert result["ok"] is False
        assert "workflow-backend unreachable" in result["error"]


def _run(coro):
    """Run *coro* to completion synchronously (stand-in for the real
    run_async bridge, which needs a live agent loop or asyncio.run)."""
    import asyncio

    return asyncio.run(coro)
