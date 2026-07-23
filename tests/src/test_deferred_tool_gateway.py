"""Unit tests for src/services/deferred_tool_gateway.py.

Coverage:
  - register() + resolve() unblocks wait_for_response() with the real result
  - wait_for_response() times out and returns None when never resolved
  - resolve() on an unknown call_id returns False
  - has_pending() reflects registered/resolved state
  - clear_session() cancels all pending entries for a session with an error
    result, and drops them from the index
  - concurrent sessions don't leak into each other's has_pending/clear_session
"""

from __future__ import annotations

import threading
import time

import pytest

from src.services import deferred_tool_gateway as gw


def test_register_and_resolve_unblocks_wait():
    entry = gw.register("call-1", "session-a", "read_file", {"path": "a.py"})
    assert entry.tool == "read_file"

    result_holder = {}

    def _waiter():
        result_holder["result"] = gw.wait_for_response("call-1", timeout=5)

    t = threading.Thread(target=_waiter)
    t.start()
    time.sleep(0.1)  # let the waiter block first
    resolved = gw.resolve("call-1", {"ok": True, "content": "print('hi')"})
    t.join(timeout=5)

    assert resolved is True
    assert result_holder["result"] == {"ok": True, "content": "print('hi')"}


def test_wait_for_response_times_out():
    gw.register("call-timeout", "session-b", "read_file", {"path": "x.py"})
    start = time.monotonic()
    result = gw.wait_for_response("call-timeout", timeout=0.3)
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 2  # doesn't hang past the timeout


def test_wait_for_response_unknown_call_id_returns_none_immediately():
    start = time.monotonic()
    result = gw.wait_for_response("never-registered", timeout=5)
    elapsed = time.monotonic() - start

    assert result is None
    assert elapsed < 1


def test_resolve_unknown_call_id_returns_false():
    assert gw.resolve("nonexistent", {"ok": True}) is False


def test_resolve_rejects_mismatched_session():
    entry = gw.register("call-mismatch", "session-owner", "read_file", {"path": "a"})
    resolved = gw.resolve("call-mismatch", {"ok": True}, session_key="session-attacker")
    assert resolved is False
    assert not entry.event.is_set()
    # Clean up — the real owner can still resolve it.
    assert gw.resolve("call-mismatch", {"ok": True}, session_key="session-owner") is True
    # Drain it — resolve() alone doesn't pop the entry (only a waiter does);
    # an undrained entry would otherwise leak into other tests' tool-name
    # lookups for the rest of the process's lifetime (it did — see #Gap
    # found via a real cross-file hang while adding the MCP bridge tests).
    gw.wait_for_response("call-mismatch", timeout=0)


def test_resolve_without_session_key_skips_check():
    gw.register("call-no-check", "session-owner2", "read_file", {"path": "a"})
    assert gw.resolve("call-no-check", {"ok": True}) is True
    gw.wait_for_response("call-no-check", timeout=0)  # drain — see note above


def test_has_pending_reflects_registration_and_resolution():
    assert gw.has_pending("session-c") is False
    gw.register("call-c1", "session-c", "write_file", {"path": "y.py"})
    assert gw.has_pending("session-c") is True
    gw.resolve("call-c1", {"ok": True})
    # wait_for_response pops the entry on resolution — but resolve() alone
    # (without a waiter draining it) leaves it registered until waited on.
    # Drain it explicitly to mirror real usage (the MCP handler always waits).
    gw.wait_for_response("call-c1", timeout=1)
    assert gw.has_pending("session-c") is False


def test_clear_session_cancels_pending_with_error_result():
    entry_a = gw.register("call-d1", "session-d", "run_command", {"command": "ls"})
    entry_b = gw.register("call-d2", "session-d", "git_status", {})

    cancelled = gw.clear_session("session-d")
    assert cancelled == 2

    # Both entries resolved with an error result and their events set.
    assert entry_a.event.is_set()
    assert entry_b.event.is_set()
    assert entry_a.result == {"ok": False, "error": "session ended before the IDE responded"}

    assert gw.has_pending("session-d") is False


def test_clear_session_empty_session_returns_zero():
    assert gw.clear_session("session-never-existed") == 0


def test_sessions_do_not_leak_into_each_other():
    gw.register("call-e1", "session-e", "read_file", {"path": "a"})
    gw.register("call-f1", "session-f", "read_file", {"path": "b"})

    assert gw.has_pending("session-e") is True
    assert gw.has_pending("session-f") is True

    gw.clear_session("session-e")

    assert gw.has_pending("session-e") is False
    assert gw.has_pending("session-f") is True

    # Clean up session-f so it doesn't leak into other test runs.
    gw.clear_session("session-f")


def test_get_deferred_tool_timeout_default(monkeypatch):
    monkeypatch.delenv("HERMES_DEFERRED_TOOL_TIMEOUT", raising=False)
    assert gw.get_deferred_tool_timeout() == 300


def test_get_deferred_tool_timeout_override(monkeypatch):
    monkeypatch.setenv("HERMES_DEFERRED_TOOL_TIMEOUT", "60")
    assert gw.get_deferred_tool_timeout() == 60


def test_get_deferred_tool_timeout_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("HERMES_DEFERRED_TOOL_TIMEOUT", "not-a-number")
    assert gw.get_deferred_tool_timeout() == 300


def test_translator_registry_round_trip():
    sentinel = object()
    assert gw.get_translator("session-g") is None
    gw.register_translator("session-g", sentinel)
    assert gw.get_translator("session-g") is sentinel
    gw.unregister_translator("session-g")
    assert gw.get_translator("session-g") is None


def test_unregister_translator_missing_session_is_a_noop():
    gw.unregister_translator("session-never-registered")  # must not raise


# ---------------------------------------------------------------------------
# await_response — pure async poll, no thread pool involved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_await_response_resolves_from_another_thread():
    gw.register("call-async-1", "session-async", "read_file", {"path": "a"})

    def _resolver():
        time.sleep(0.05)
        gw.resolve("call-async-1", {"ok": True, "content": "hi"})

    threading.Thread(target=_resolver, daemon=True).start()
    result = await gw.await_response("call-async-1", timeout=5)
    assert result == {"ok": True, "content": "hi"}


@pytest.mark.asyncio
async def test_await_response_times_out():
    gw.register("call-async-2", "session-async2", "read_file", {"path": "a"})
    result = await gw.await_response("call-async-2", timeout=0.3)
    assert result is None
