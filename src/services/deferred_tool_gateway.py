"""Gateway-side deferred-tool primitive (blocking event-based queue).

Mirrors ``vendor/hermes-agent/tools/clarify_gateway.py``'s register/wait/
resolve shape, but for a tool CALL/RESULT instead of a question/answer: the
IDE-bridge MCP server (``src/mcp/coding_bridge_server.py``) needs to publish
a ``hermes.tool.deferred`` event for the IDE extension to execute, then
block the MCP tool-call thread until the IDE reports back the real result —
same "block a worker thread on an Event, resolved by a different request"
problem clarify already solves, just carrying a tool result instead of a
free-text answer.

State is module-level so the tool-result endpoint (see
``src/api/routers/chat.py``) can resolve a pending wait without holding a
reference to whichever MCP tool call registered it.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------


@dataclass
class _DeferredToolEntry:
    """One pending deferred tool call inside a coding session."""

    call_id: str
    session_key: str
    tool: str
    params: dict[str, Any]
    event: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


_lock = threading.RLock()
# call_id -> _DeferredToolEntry (primary lookup for the tool-result endpoint)
_entries: dict[str, _DeferredToolEntry] = {}
# session_key -> list[call_id] (for session-boundary cleanup)
_session_index: dict[str, list[str]] = {}

# Distinguishes "not resolved yet" from a legitimate falsy/None result inside
# await_response()'s poll loop.
_SENTINEL_NOT_READY = object()


# ---------------------------------------------------------------------------
# Public API — MCP tool-handler side
# ---------------------------------------------------------------------------


def register(
    call_id: str,
    session_key: str,
    tool: str,
    params: dict[str, Any],
) -> _DeferredToolEntry:
    """Register a pending deferred tool call and return the entry.

    The caller (an MCP tool handler in coding_bridge_server.py) publishes
    the ``hermes.tool.deferred`` event to the IDE, then blocks on
    ``wait_for_response(call_id, timeout)``.
    """
    entry = _DeferredToolEntry(
        call_id=call_id, session_key=session_key, tool=tool, params=dict(params)
    )
    with _lock:
        _entries[call_id] = entry
        _session_index.setdefault(session_key, []).append(call_id)
    return entry


def _pop_entry(call_id: str, session_key: str) -> None:
    """Drop a resolved/expired entry from both indices."""
    with _lock:
        _entries.pop(call_id, None)
        ids = _session_index.get(session_key)
        if ids and call_id in ids:
            ids.remove(call_id)
            if not ids:
                _session_index.pop(session_key, None)


def wait_for_response(call_id: str, timeout: float) -> dict[str, Any] | None:
    """Block the CALLING THREAD on the entry's event until resolved or timeout.

    Polls in 1-second slices (mirrors clarify_gateway) rather than a single
    blocking ``Event.wait(timeout=...)`` call, so a long wait doesn't look
    like total inactivity to anything watching this thread. For async
    callers, use ``await_response`` instead — it does NOT hand this
    function to a thread pool (see that function's docstring for why).

    Returns the resolved result dict, or ``None`` on timeout / unknown id.
    """
    with _lock:
        entry = _entries.get(call_id)
    if entry is None:
        return None

    deadline = time.monotonic() + max(timeout, 0.0)
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        if entry.event.wait(timeout=min(1.0, remaining)):
            break

    _pop_entry(call_id, entry.session_key)
    return entry.result


async def await_response(call_id: str, timeout: float) -> dict[str, Any] | None:
    """Async wait for the entry's result — polls, never blocks the event loop.

    Deliberately NOT ``loop.run_in_executor(some_pool, wait_for_response,
    ...)``: bridging a `threading.Event` back onto a specific event loop via
    an executor ties the wait to that loop's lifetime, and a long-lived pool
    reused across many short-lived loops (e.g. one per test, or one per
    request in some server setups) risks a wait's completion callback firing
    against a loop that's already gone — the failure mode isn't a clean
    error, it's the *awaiting* coroutine hanging forever, since nothing ever
    reschedules it. A plain async poll has no such cross-loop dependency:
    every ``asyncio.sleep`` reschedules on whichever loop is actually running
    this coroutine right now.
    """
    deadline = time.monotonic() + max(timeout, 0.0)
    poll_interval = 0.25
    while True:
        with _lock:
            entry = _entries.get(call_id)
            if entry is None:
                return None
            if entry.event.is_set():
                result = entry.result
                session_key = entry.session_key
            else:
                result = _SENTINEL_NOT_READY
                session_key = None

        if result is not _SENTINEL_NOT_READY:
            _pop_entry(call_id, session_key)
            return result

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _pop_entry(call_id, entry.session_key)
            return None
        await asyncio.sleep(min(poll_interval, remaining))


# ---------------------------------------------------------------------------
# Public API — IDE tool-result endpoint side
# ---------------------------------------------------------------------------


def resolve(
    call_id: str,
    result: dict[str, Any],
    *,
    session_key: str | None = None,
) -> bool:
    """Unblock the MCP tool-call thread waiting on ``call_id``.

    When *session_key* is given (the tool-result endpoint always passes the
    caller's own session id), the entry must belong to that session — this
    stops one coding session from resolving (or discovering the existence
    of) another session's pending tool call.

    Returns True if a matching entry was found and resolved, False otherwise
    (already resolved, expired, never existed, or session mismatch).
    """
    with _lock:
        entry = _entries.get(call_id)
        if entry is None:
            return False
        if session_key is not None and entry.session_key != session_key:
            return False
    entry.result = dict(result) if result is not None else {}
    entry.event.set()
    return True


def has_pending(session_key: str) -> bool:
    """Return True when this session has at least one pending deferred call."""
    with _lock:
        ids = _session_index.get(session_key) or []
        return any(_entries.get(cid) is not None for cid in ids)


def clear_session(session_key: str) -> int:
    """Resolve (with an empty result) and drop every pending call for a session.

    Used by session-boundary cleanup so a blocked MCP tool-call thread
    doesn't hang past the end of its coding session. Returns the number of
    entries cancelled.
    """
    with _lock:
        ids = list(_session_index.pop(session_key, []) or [])
        entries = [_entries.pop(cid, None) for cid in ids]
    cancelled = 0
    for entry in entries:
        if entry is None:
            continue
        entry.result = {"ok": False, "error": "session ended before the IDE responded"}
        entry.event.set()
        cancelled += 1
    return cancelled


# ---------------------------------------------------------------------------
# Per-session SSE translator registry (opencode-backed turns only)
# ---------------------------------------------------------------------------
# The MCP bridge (src/mcp/coding_bridge_server.py) runs each tool call inside
# its own async request handler — a different execution context than the
# worker thread that drives /coding/chat's translator for the existing
# Hermes-backed path. This registry lets a bridge tool handler find that
# session's live translator and push hermes.tool.progress/deferred events
# into the SAME SSE stream the IDE's HTTP response is already reading,
# mirroring what _run_coding_agent_turn does directly for Hermes turns.

_translators: dict[str, Any] = {}
_translators_lock = threading.RLock()


def register_translator(session_id: str, translator: Any) -> None:
    """Register the live SSE translator for an in-flight opencode-backed turn."""
    with _translators_lock:
        _translators[session_id] = translator


def unregister_translator(session_id: str) -> None:
    """Drop the translator registration once the turn ends."""
    with _translators_lock:
        _translators.pop(session_id, None)


def get_translator(session_id: str) -> Any | None:
    """Return the live translator for *session_id*, or None if not registered."""
    with _translators_lock:
        return _translators.get(session_id)


def get_deferred_tool_timeout() -> int:
    """Read the deferred-tool response timeout (seconds).

    Defaults to 300 (5 minutes) — long enough for a real file/git/terminal
    round trip through the IDE extension, short enough that a genuinely
    abandoned session eventually unblocks the MCP tool-call thread instead
    of hanging it forever. Override with ``HERMES_DEFERRED_TOOL_TIMEOUT``.
    """
    import os

    try:
        return int(os.environ.get("HERMES_DEFERRED_TOOL_TIMEOUT", "300"))
    except ValueError:
        return 300
