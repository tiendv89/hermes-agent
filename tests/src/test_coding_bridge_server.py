"""Unit tests for src/mcp/coding_bridge_server.py.

Coverage:
  - build_bridge_app() registers all 14 coding tools
  - a validation error (e.g. missing required path) returns immediately,
    without ever registering a deferred-tool wait
  - a valid call defers to the IDE and blocks until resolved, returning the
    IDE's real result
  - the session's live translator (if registered) gets on_tool_start /
    on_tool_complete with the deferred marker shape
  - a call with no live translator registered still defers (just logs a
    warning) rather than crashing
  - an unresolved call times out and returns an ok:false error
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
from unittest.mock import MagicMock

import pytest

from src.mcp.coding_bridge_server import build_bridge_app

gw = importlib.import_module("src.services.deferred_tool_gateway")


def _ensure_real_plugins_package() -> None:
    """Undo a stale, non-package ``plugins`` stub left by an earlier test.

    Several sibling test files (e.g. ``test_cancel.py``) do
    ``sys.modules["plugins"] = types.ModuleType("plugins")`` — a bare,
    non-package placeholder — if ``"plugins" not in sys.modules`` yet, and
    never clean it up. That's harmless to THEM (they only ever reach for
    ``plugins.context``/``plugins.skills``, stubbed alongside it), but the
    tool handlers this file exercises resolve via ``coding_bridge_server.py``'s
    function-local ``from plugins.tools.local_file_ops import ...`` at CALL
    time, which needs the REAL ``plugins`` package (a real ``__path__``) to
    find the real ``plugins.tools`` submodule — a bare stub makes every one
    of that import raise ``ModuleNotFoundError: ... 'plugins' is not a
    package``. Same class of cross-file sys.modules pollution fixed
    elsewhere in this suite (see test_vcs_service_client.py's
    ``_ensure_plugins_context()``), just for the whole package instead of
    one submodule.
    """
    existing = sys.modules.get("plugins")
    if existing is not None and hasattr(existing, "__path__"):
        return
    for name in list(sys.modules):
        if name == "plugins" or name.startswith("plugins."):
            del sys.modules[name]
    importlib.import_module("plugins")


@pytest.fixture(autouse=True)
def _real_plugins_package():
    _ensure_real_plugins_package()
    yield


@pytest.fixture(autouse=True)
def _fresh_gateway_module():
    """Re-bind ``gw`` to the current ``src.services.deferred_tool_gateway``
    before every test.

    Several sibling test files in this suite deliberately evict every
    ``sys.modules`` key starting with ``"src"`` between tests (see e.g.
    ``tests/plugins/test_move_feature.py``'s autouse fixture), to force
    fresh per-test reimports of env-var-sensitive modules. That means the
    module object this file's top-level import bound at collection time can
    become a stale, orphaned duplicate of whatever
    ``coding_bridge_server.py``'s function-local ``from src.services import
    deferred_tool_gateway`` re-resolves to later — two independent copies of
    the same module, each with its own empty ``_entries`` dict. Concretely:
    a background thread here would try to resolve a call the real handler
    registered on the OTHER copy, never find it, and the real await would
    hang for the full ``HERMES_DEFERRED_TOOL_TIMEOUT`` (default 300s) —
    this was reproduced for real while adding this test file to the suite,
    not a hypothetical. Re-importing at the start of every test keeps this
    module's ``gw`` pointed at whatever's current for that test's run.
    """
    global gw
    gw = importlib.import_module("src.services.deferred_tool_gateway")
    yield


async def _call(app, name, arguments):
    return await app.call_tool(name, arguments)


def _resolve_soon(tool_name: str, result: dict, delay: float = 0.05) -> None:
    """Background thread: find the just-registered call for `tool_name` and resolve it.

    Filters on ``not entry.event.is_set()`` — other tests in this process
    register+resolve entries directly without a waiter ever draining them
    (register()/resolve() alone don't pop the entry; only wait_for_response/
    await_response do), so an already-resolved, same-tool-named entry can
    legitimately still be sitting in the global registry. Matching on tool
    name alone once caused this to "resolve" a stale leftover entry instead
    of the one this call actually registered, leaving the real wait to time
    out after minutes.

    Captures ``gw`` (the module reference, rebound fresh per-test by the
    ``_fresh_gateway_module`` autouse fixture above) at call time so the
    worker thread and the tool handler it's racing against are guaranteed
    to be looking at the same registry.
    """
    module_ref = gw

    def _worker():
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with module_ref._lock:
                for call_id, entry in list(module_ref._entries.items()):
                    if entry.tool == tool_name and not entry.event.is_set():
                        module_ref.resolve(call_id, result, session_key=entry.session_key)
                        return
            time.sleep(0.02)

    threading.Thread(target=_worker, daemon=True).start()


class TestToolRegistration:
    @pytest.mark.asyncio
    async def test_all_tools_registered(self):
        app = build_bridge_app("session-list")
        tools = await app.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "read_file",
            "edit_file",
            "write_file",
            "create_directory",
            "browse_directory",
            "search_code",
            "search_files",
            "run_command",
            "git_status",
            "git_diff",
            "git_commit",
            "git_push",
            "git_checkout",
            "git_log",
        }


class TestValidationShortCircuit:
    @pytest.mark.asyncio
    async def test_missing_path_returns_immediately(self):
        app = build_bridge_app("session-validate")
        result = await _call(app, "read_file", {"path": ""})
        text = result[0].text
        assert "path is required" in text
        # Nothing should have been registered as a pending deferred call.
        assert gw.has_pending("session-validate") is False

    @pytest.mark.asyncio
    async def test_missing_edits_returns_immediately(self):
        app = build_bridge_app("session-validate-2")
        result = await _call(app, "edit_file", {"path": "a.py", "edits": []})
        assert "edits is required" in result[0].text
        assert gw.has_pending("session-validate-2") is False


class TestDeferAndWait:
    @pytest.mark.asyncio
    async def test_valid_call_defers_and_returns_ide_result(self):
        session_id = "session-defer-1"
        app = build_bridge_app(session_id)

        _resolve_soon("read_file", {"ok": True, "content": "print('hi')\n"})

        result = await _call(app, "read_file", {"path": "main.py"})
        assert "print" in result[0].text
        assert gw.has_pending(session_id) is False

    @pytest.mark.asyncio
    async def test_translator_gets_tool_start_and_complete(self):
        session_id = "session-defer-2"
        app = build_bridge_app(session_id)
        translator = MagicMock()
        gw.register_translator(session_id, translator)
        try:
            _resolve_soon("write_file", {"ok": True})
            await _call(app, "write_file", {"path": "new.py", "content": "x = 1"})
        finally:
            gw.unregister_translator(session_id)

        translator.on_tool_start.assert_called_once()
        translator.on_tool_complete.assert_called_once()
        _, kwargs = translator.on_tool_complete.call_args
        assert kwargs["output"]["__deferred__"] is True
        assert kwargs["output"]["tool"] == "write_file"
        assert kwargs["output"]["params"] == {"path": "new.py", "content": "x = 1"}

    @pytest.mark.asyncio
    async def test_no_live_translator_does_not_crash(self):
        session_id = "session-defer-3"
        app = build_bridge_app(session_id)
        assert gw.get_translator(session_id) is None

        _resolve_soon("git_status", {"ok": True, "branch": "main"})
        result = await _call(app, "git_status", {})
        assert "main" in result[0].text


class TestTimeout:
    @pytest.mark.asyncio
    async def test_unresolved_call_times_out(self, monkeypatch):
        monkeypatch.setenv("HERMES_DEFERRED_TOOL_TIMEOUT", "1")
        session_id = "session-timeout"
        app = build_bridge_app(session_id)

        result = await _call(app, "git_status", {})
        text = result[0].text
        assert "timed out" in text
        assert gw.has_pending(session_id) is False
