"""Tests for the pre-dispatch guardrail gate added to plugins/__init__.py (T2).

Verifies that _guardrail_wrapper:
  - Blocks tool calls that violate guardrail rules (G1, G6, G8, G10, G11).
  - Allows legitimate tool calls through to the underlying handler.
  - Returns a structured refusal JSON string on block.
  - Works for both sync and async tool handlers.
  - Skips G10 when no session context is set.
  - Disables all checks when HERMES_GUARDRAILS_ENABLED=0.
  - Handles empty/None arguments without crashing.
  - Allows unknown tool names by default.
  - Does not break the existing JSON-string wrapper contract.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers: load the plugins __init__ module and its sub-modules fresh
# ---------------------------------------------------------------------------


def _load_plugins() -> Any:
    """Load plugins/__init__.py fresh (avoid cross-test module cache pollution)."""
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
    """Reset plugins modules between tests to avoid module-level cache pollution."""
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Unset service URL env vars so check_available() gates return False."""
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("HERMES_GUARDRAILS_ENABLED", raising=False)
    yield


@pytest.fixture(autouse=True)
def _clear_thread_local():
    """Clear the plugins.context thread-local between tests."""
    yield
    try:
        from plugins.context import _local

        for attr in ("workspace_id", "feature_id", "user_id", "org_id", "agent_session_id"):
            if hasattr(_local, attr):
                setattr(_local, attr, "")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Helpers for extracting _guardrail_wrapper and _get_session_context
# ---------------------------------------------------------------------------


def _get_guardrail_wrapper(plugins_mod: Any) -> Any:
    return plugins_mod._guardrail_wrapper


def _get_get_session_context(plugins_mod: Any) -> Any:
    return plugins_mod._get_session_context


# ---------------------------------------------------------------------------
# Unit tests for _get_session_context
# ---------------------------------------------------------------------------


class TestGetSessionContext:
    def test_returns_none_when_no_context_set(self):
        plugins_mod = _load_plugins()
        fn = _get_get_session_context(plugins_mod)
        result = fn()
        assert result is None

    def test_returns_context_when_workspace_set(self):
        plugins_mod = _load_plugins()
        import plugins.context as ctx

        ctx.set_context("sess-1", "my-workspace", "feat-1")
        ctx.set_agent_context("sess-1", None)  # set agent_session_id on thread-local
        fn = _get_get_session_context(plugins_mod)
        result = fn()
        assert result is not None
        assert result["workspace_id"] == "my-workspace"
        assert result["feature_id"] == "feat-1"

    def test_returns_none_when_no_agent_session(self):
        """Without agent_session_id set, _get_session_context returns None.

        This replaces the old test that checked thread-local workspace_id directly.
        _get_session_context now uses get_agent_session_id() + get_context_for_session()
        so that it reads from the per-session store, not the stale thread-local.
        """
        plugins_mod = _load_plugins()
        # No set_agent_context call — agent_session_id is empty
        fn = _get_get_session_context(plugins_mod)
        result = fn()
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests for _guardrail_wrapper — sync handlers
# ---------------------------------------------------------------------------


class TestGuardrailWrapperSync:
    def _make_handler(self, return_value: Any = None):
        """Create a simple mock sync handler."""
        call_log = []

        def handler(*args, **kwargs):
            call_log.append({"args": args, "kwargs": kwargs})
            if return_value is not None:
                return return_value
            return {"ok": True, "data": "from handler"}

        handler.call_log = call_log
        return handler

    def test_allowed_tool_calls_handler(self, monkeypatch):
        """An allowed tool call should invoke the underlying handler."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "read_file", is_async=False)

        # read_file with safe arguments is allowed
        wrapped({"document": "product_spec"})
        assert inner.call_log, "handler was not called"

    def test_blocked_tool_does_not_call_handler(self, monkeypatch):
        """A guardrail-blocked tool call must NOT invoke the handler."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "delete_file", is_async=False)

        wrapped({"path": "some-file.md"})
        assert not inner.call_log, "handler was called despite guardrail block"

    def test_blocked_returns_json_string(self, monkeypatch):
        """Blocked tool calls return a JSON-encoded refusal string."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "delete_file", is_async=False)

        result = wrapped({"path": "data.json"})
        assert isinstance(result, str), "result must be a string"
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "deletion_blocked"
        assert parsed["tool"] == "delete_file"

    def test_refusal_has_guardrail_id(self, monkeypatch):
        """Refusal message must include a guardrail_id field."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "delete_file", is_async=False)

        parsed = json.loads(wrapped({}))
        assert "guardrail" in parsed
        assert parsed["guardrail"]  # not empty

    def test_g6_transition_blocked(self, monkeypatch):
        """approve_feature(stage=handoff) is blocked by G6."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "approve_feature", is_async=False)

        result = wrapped({"stage": "handoff"})
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "transition_blocked"
        assert not inner.call_log

    def test_g6_approve_allowed_for_product_spec(self, monkeypatch):
        """approve_feature(stage=product_spec) is allowed by G6."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "approve_feature", is_async=False)

        wrapped({"stage": "product_spec"})
        assert inner.call_log, "handler not called for allowed transition"

    def test_g8_xss_in_write_file_blocked(self, monkeypatch):
        """write_file with XSS content is blocked by G8."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "write_file", is_async=False)

        result = wrapped({"path": "notes.md", "content": "<script>alert(1)</script>"})
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "content_sanitization_blocked"
        assert not inner.call_log

    def test_g8_clean_content_allowed(self, monkeypatch):
        """write_file with clean markdown content is allowed by G8."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "write_file", is_async=False)

        wrapped({"path": "notes.md", "content": "# My notes\n\nClean content."})
        assert inner.call_log, "handler not called for clean content"

    def test_g11_write_claude_md_blocked(self, monkeypatch):
        """write_file targeting CLAUDE.md is blocked by G11."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "write_file", is_async=False)

        result = wrapped({"path": "CLAUDE.md", "content": "new rules"})
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "system_prompt_source_blocked"
        assert not inner.call_log

    def test_g10_cross_workspace_blocked(self, monkeypatch):
        """Tool call with mismatched workspace_id is blocked by G10."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")
        import plugins.context as ctx

        ctx.set_context("sess-a", "workspace-A", "feat-1")
        ctx.set_agent_context("sess-a", None)  # wire agent_session_id so _get_session_context resolves

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "read_file", is_async=False)

        result = wrapped({"workspace_id": "workspace-B"})
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "cross_workspace_blocked"
        assert not inner.call_log

    def test_g10_matching_workspace_allowed(self, monkeypatch):
        """Tool call with matching workspace_id is allowed by G10."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")
        import plugins.context as ctx

        ctx.set_context("sess-a", "workspace-A", "feat-1")
        ctx.set_agent_context("sess-a", None)  # wire agent_session_id so _get_session_context resolves

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "read_file", is_async=False)

        wrapped({"workspace_id": "workspace-A"})
        assert inner.call_log, "handler not called despite matching workspace"

    def test_g10_no_session_context_skips_check(self, monkeypatch):
        """Without a session context, G10 is skipped — tool is allowed."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")
        # No set_context call — thread-local is empty

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "read_file", is_async=False)

        # workspace_id mismatch but no session context → G10 skipped
        wrapped({"workspace_id": "any-workspace"})
        assert inner.call_log, "handler not called when G10 should be skipped"

    def test_disabled_guardrails_allow_all(self, monkeypatch):
        """When HERMES_GUARDRAILS_ENABLED=0, all guardrails are bypassed."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "0")

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "delete_file", is_async=False)

        # G1 would normally block this
        wrapped({"path": "file.txt"})
        assert inner.call_log, "handler not called despite guardrails disabled"

    def test_disabled_xss_passes_through(self, monkeypatch):
        """When guardrails disabled, XSS content write is allowed through."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "0")

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "write_file", is_async=False)

        wrapped({"path": "f.md", "content": "<script>evil()</script>"})
        assert inner.call_log

    def test_empty_arguments_handled(self, monkeypatch):
        """Empty arguments dict must not cause a crash or false block."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "get_workspace_context", is_async=False)

        # Must not raise
        wrapped({})
        assert inner.call_log

    def test_none_first_arg_handled(self, monkeypatch):
        """None as first arg (non-dict) is treated as empty arguments."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "get_workspace_context", is_async=False)

        # args[0] is None — should not crash
        wrapped(None)
        # get_workspace_context with no args → allowed, handler called
        assert inner.call_log

    def test_kwargs_only_call_handled(self, monkeypatch):
        """Keyword-only calls (no positional dict) treat arguments as empty."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "read_file", is_async=False)

        # No positional dict — arguments = {} → guardrail check with empty args → allowed
        wrapped(document="product_spec", workspace_id="ws-1")
        assert inner.call_log

    def test_unknown_tool_name_allowed(self, monkeypatch):
        """An unknown (future) tool name is allowed by default."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "future_unknown_tool", is_async=False)

        wrapped({"some_arg": "value"})
        assert inner.call_log, "unknown tool was blocked instead of allowed by default"

    def test_handler_return_value_passed_through(self, monkeypatch):
        """The handler's return value is passed through unchanged on allow."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        sentinel = "SENTINEL_RETURN_VALUE"
        inner = self._make_handler(sentinel)
        wrapped = plugins_mod._guardrail_wrapper(inner, "read_file", is_async=False)

        result = wrapped({"document": "product_spec"})
        assert result == sentinel

    def test_g9_cta_phishing_blocked(self, monkeypatch):
        """suggest_next_actions with lifecycle-mutating action_text is blocked by G9."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(
            inner, "suggest_next_actions", is_async=False
        )

        args = {
            "suggestions": [
                {
                    "id": "s1",
                    "title": "Approve all",
                    "action_text": "approve_feature(stage='handoff')",
                }
            ]
        }
        result = wrapped(args)
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "cta_phishing_blocked"
        assert not inner.call_log

    def test_g3_env_disclosure_blocked_on_path(self, monkeypatch):
        """Reading a .env file path is blocked by G3."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "read_file", is_async=False)

        result = wrapped({"path": ".env"})
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "env_disclosure_blocked"
        assert not inner.call_log

    def test_g6_github_pr_approve_blocked(self, monkeypatch):
        """github_pr_review with event=APPROVE is blocked by G6."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler()
        wrapped = plugins_mod._guardrail_wrapper(
            inner, "github_pr_review", is_async=False
        )

        result = wrapped(
            {"event": "APPROVE", "pr_url": "https://github.com/a/b/pull/1", "body": "LGTM"}
        )
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "pr_approve_blocked"
        assert not inner.call_log

    def test_g6_github_pr_request_changes_allowed(self, monkeypatch):
        """github_pr_review with event=REQUEST_CHANGES is allowed by G6."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(
            inner, "github_pr_review", is_async=False
        )

        wrapped({"event": "REQUEST_CHANGES", "pr_url": "...", "body": "needs work"})
        assert inner.call_log


# ---------------------------------------------------------------------------
# Unit tests for _guardrail_wrapper — async handlers
# ---------------------------------------------------------------------------


class TestGuardrailWrapperAsync:
    def _make_async_handler(self, return_value: Any = None):
        """Create a mock async handler."""
        call_log = []

        async def handler(*args, **kwargs):
            call_log.append({"args": args, "kwargs": kwargs})
            return return_value or {"ok": True, "data": "async result"}

        handler.call_log = call_log
        return handler

    @pytest.mark.asyncio
    async def test_async_blocked_tool_does_not_call_handler(self, monkeypatch):
        """Blocked async tool does not invoke the handler."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_async_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "delete_file", is_async=True)

        result = await wrapped({"path": "data.db"})
        assert not inner.call_log
        parsed = json.loads(result)
        assert parsed["ok"] is False

    @pytest.mark.asyncio
    async def test_async_allowed_tool_calls_handler(self, monkeypatch):
        """Allowed async tool invokes the handler and returns its result."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        inner = self._make_async_handler({"ok": True, "results": []})
        wrapped = plugins_mod._guardrail_wrapper(inner, "query_gitnexus", is_async=True)

        result = await wrapped({"query": "where is register"})
        assert inner.call_log, "async handler was not called"
        assert result == {"ok": True, "results": []}

    @pytest.mark.asyncio
    async def test_async_disabled_allows_blocked_tool(self, monkeypatch):
        """HERMES_GUARDRAILS_ENABLED=0 disables async guardrail gate."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "0")

        inner = self._make_async_handler({"ok": True})
        wrapped = plugins_mod._guardrail_wrapper(inner, "delete_file", is_async=True)

        await wrapped({})
        assert inner.call_log

    @pytest.mark.asyncio
    async def test_async_g10_cross_workspace_blocked(self, monkeypatch):
        """Async cross-workspace tool call is blocked by G10."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")
        import plugins.context as ctx

        ctx.set_context("sess-async", "workspace-X", "feat-1")
        ctx.set_agent_context("sess-async", None)  # wire agent_session_id so _get_session_context resolves

        inner = self._make_async_handler()
        wrapped = plugins_mod._guardrail_wrapper(inner, "query_rag", is_async=True)

        result = await wrapped({"workspace_id": "workspace-Y", "query": "auth"})
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "cross_workspace_blocked"
        assert not inner.call_log


# ---------------------------------------------------------------------------
# Integration: _guardrail_wrapper + _json_result_handler chain
# (verifies the full dispatch chain as registered tools use it)
# ---------------------------------------------------------------------------


class TestDispatchChainIntegration:
    def _wrap_handler(
        self, plugins_mod: Any, tool_name: str, handler: Any, is_async: bool = False
    ) -> Any:
        """Apply both wrappers as register() does."""
        json_handler = plugins_mod._json_result_handler(handler, is_async)
        return plugins_mod._guardrail_wrapper(json_handler, tool_name, is_async)

    def test_blocked_returns_json_string_from_chain(self, monkeypatch):
        """Full chain: blocked tool returns valid JSON string with refusal data."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        handler = MagicMock(return_value={"ok": True})
        wrapped = self._wrap_handler(plugins_mod, "delete_workspace", handler)

        result = wrapped({"id": "ws-1"})
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed["ok"] is False
        assert "deletion_blocked" in parsed["reason_code"]
        handler.assert_not_called()

    def test_allowed_handler_result_json_encoded(self, monkeypatch):
        """Full chain: allowed handler result is JSON-encoded by _json_result_handler."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        expected = {"ok": True, "data": "some result"}
        handler = MagicMock(return_value=expected)
        wrapped = self._wrap_handler(plugins_mod, "get_workspace_context", handler)

        result = wrapped({"workspace_id": "ws-1"})
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed == expected

    def test_args_dict_unpacked_for_handler(self, monkeypatch):
        """Tool args dict passed as first positional arg is unpacked to kwargs for handler."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        call_kwargs: dict = {}

        def handler(**kwargs):
            call_kwargs.update(kwargs)
            return {"ok": True}

        wrapped = self._wrap_handler(plugins_mod, "read_file", handler)
        wrapped({"document": "product_spec", "workspace_id": "ws-1"})

        assert call_kwargs.get("document") == "product_spec"
        assert call_kwargs.get("workspace_id") == "ws-1"

    @pytest.mark.asyncio
    async def test_async_allowed_result_returned_from_chain(self, monkeypatch):
        """Full async chain: allowed async handler result is returned correctly."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")

        expected = {"ok": True, "results": ["item1"]}

        async def handler(**kwargs):
            return expected

        json_handler = plugins_mod._json_result_handler(handler, is_async=True)
        wrapped = plugins_mod._guardrail_wrapper(json_handler, "query_rag", is_async=True)

        result = await wrapped({"query": "auth flow"})
        assert isinstance(result, str)
        parsed = json.loads(result)
        assert parsed == expected

    def test_disabled_full_chain_passes_blocked_tool(self, monkeypatch):
        """With guardrails off, even a 'delete' tool invokes the handler."""
        plugins_mod = _load_plugins()
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "0")

        handler = MagicMock(return_value={"ok": True, "deleted": 1})
        wrapped = self._wrap_handler(plugins_mod, "delete_all", handler)

        result = wrapped({})
        handler.assert_called_once()
        parsed = json.loads(result)
        assert parsed["ok"] is True


# ---------------------------------------------------------------------------
# Integration: register() applies guardrail wrappers to all tools
# ---------------------------------------------------------------------------


class TestRegisterAppliesGuardrails:
    """Verify that register() now applies guardrail wrappers and the handlers
    still return JSON strings (regression check for existing contract)."""

    def test_registered_handler_returns_json_string(self, monkeypatch):
        """The registered tool handler must still JSON-stringify the dict return."""
        import json as _json

        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")
        plugins_mod = _load_plugins()
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
        out = wrapped(content="clean content", workspace_id="ws", feature_id="f")
        assert isinstance(out, str)
        assert _json.loads(out)["ok"] is False

    def test_registered_handler_blocks_xss_content(self, monkeypatch):
        """Registered write_file handler must block XSS content via guardrail."""
        import json as _json

        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")
        plugins_mod = _load_plugins()
        ctx = MagicMock()
        plugins_mod.register(ctx)

        write_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "write_file"
        )
        wrapped = write_call.kwargs["handler"]

        # XSS content in write_file args — guardrail must block before handler runs
        out = wrapped(
            {"path": "notes.md", "content": "<script>alert(1)</script>"},
        )
        assert isinstance(out, str)
        parsed = _json.loads(out)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "content_sanitization_blocked"

    def test_registered_handler_blocks_transition_on_handoff(self, monkeypatch):
        """Registered approve_feature handler must block handoff stage via G6."""
        import json as _json

        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")
        plugins_mod = _load_plugins()
        ctx = MagicMock()
        plugins_mod.register(ctx)

        approve_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "approve_feature"
        )
        wrapped = approve_call.kwargs["handler"]

        out = wrapped({"stage": "handoff"})
        assert isinstance(out, str)
        parsed = _json.loads(out)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "transition_blocked"

    def test_registered_handler_blocks_delete_tool_name(self, monkeypatch):
        """A hypothetical delete_* tool name is blocked by G1 via registered handler.

        Tests the future-proof nature of G1 — no existing tool has 'delete' in the
        name, but if one were added, _guardrail_wrapper would block it at registration.
        """
        import json as _json

        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")
        plugins_mod = _load_plugins()

        # Directly test via _guardrail_wrapper (no need to register via ctx)
        dummy_handler = MagicMock(return_value={"ok": True})
        json_handler = plugins_mod._json_result_handler(dummy_handler, is_async=False)
        guarded = plugins_mod._guardrail_wrapper(
            json_handler, "delete_workspace", is_async=False
        )

        out = guarded({})
        parsed = _json.loads(out)
        assert parsed["ok"] is False
        assert parsed["reason_code"] == "deletion_blocked"
        dummy_handler.assert_not_called()

    def test_disabled_registered_handlers_pass_through(self, monkeypatch):
        """With guardrails off, registered handlers pass through as before."""
        import json as _json

        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "0")
        plugins_mod = _load_plugins()
        ctx = MagicMock()
        plugins_mod.register(ctx)

        spec_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "write_product_spec"
        )
        wrapped = spec_call.kwargs["handler"]

        # Even with clean content; the handler returns ok:False because GITHUB_TOKEN is unset
        out = wrapped(content="clean content", workspace_id="ws", feature_id="f")
        assert isinstance(out, str)
        assert _json.loads(out)["ok"] is False  # fails for expected service reason, not guardrail

    @pytest.mark.asyncio
    async def test_registered_async_handler_returns_json_string(self, monkeypatch):
        """Registered async handler (query_rag) must still return a JSON string."""
        monkeypatch.setenv("HERMES_GUARDRAILS_ENABLED", "1")
        plugins_mod = _load_plugins()
        ctx = MagicMock()
        plugins_mod.register(ctx)

        rag_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "query_rag"
        )
        wrapped = rag_call.kwargs["handler"]
        assert rag_call.kwargs.get("is_async") is True

        import json as _json

        out = await wrapped(query="q", workspace_id="ws")
        assert isinstance(out, str)
        assert _json.loads(out)["ok"] is False  # RAG_MCP_URL not set
