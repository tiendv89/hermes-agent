"""Per-session store for the current workspace/feature context.

The gateway router calls :func:`set_context` before each agent.run_conversation()
so that:

  * the pre_llm_call hook can inject workspace/feature context into the turn
    (it resolves by session_id, which the hook receives as a kwarg), and
  * workflow plugin tool handlers can resolve workspace_id/feature_id without
    the agent passing them explicitly in every tool call (they resolve via a
    thread-local, since tool handlers don't receive the session_id).

Keying the hook lookup by session_id — rather than relying solely on a
thread-local — is deliberate: the gateway runs agent turns in a reused
ThreadPoolExecutor, so thread-local state can leak between sessions. The
session-keyed map is authoritative; the thread-local is a convenience for the
tool path only and is always overwritten at the start of each turn.

:func:`set_agent_context` stores additional per-turn data (event loop,
db_factory) needed by local tool handlers such as ``suggest_next_actions``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_by_session: dict[str, tuple[str, str, str, str]] = {}
_local = threading.local()
_context_gathered: set[str] = set()


def set_context(
    session_id: str,
    workspace_id: str,
    feature_id: str,
    user_id: str = "",
    org_id: str = "",
) -> None:
    """Record the workspace/feature IDs and caller identity for a session (and the current thread).

    Backward-compatible: absent user_id/org_id default to empty strings.
    """
    with _lock:
        _by_session[session_id] = (workspace_id, feature_id, user_id, org_id)
    _local.workspace_id = workspace_id
    _local.feature_id = feature_id
    _local.user_id = user_id
    _local.org_id = org_id
    logger.info(
        "workflow context set: session=%s workspace_id=%r feature_id=%r",
        session_id,
        workspace_id,
        feature_id,
    )


def set_agent_context(
    session_id: str,
    loop: Any,
    db_factory: Optional[Callable] = None,
) -> None:
    """Store per-turn agent context (session_id, event loop, db_factory) on the thread-local.

    Called from _run_agent_turn before the agent executes. Allows local tool
    handlers (e.g. suggest_next_actions) to perform async DB operations and
    bus.publish without needing the loop or db_factory passed as arguments.
    """
    _local.agent_session_id = session_id
    _local.agent_loop = loop
    _local.agent_db_factory = db_factory


def get_agent_session_id() -> str:
    return getattr(_local, "agent_session_id", "")


def get_agent_loop() -> Any:
    return getattr(_local, "agent_loop", None)


def get_agent_db_factory() -> Optional[Callable]:
    return getattr(_local, "agent_db_factory", None)


def clear_context(session_id: str) -> None:
    """Drop the stored context for a session once its turn completes."""
    with _lock:
        _by_session.pop(session_id, None)


def get_context_for_session(session_id: str) -> tuple[str, str]:
    """Return (workspace_id, feature_id) for a session from the per-session store only.

    Returns ("", "") when the session is not found — no thread-local fallback.
    Removing the fallback prevents stale thread-locals on reused ThreadPoolExecutor
    threads from leaking feature_id from a previous session into a new one (G2 fix).
    """
    if session_id:
        with _lock:
            found = _by_session.get(session_id)
        if found is not None:
            return found[0], found[1]
    return "", ""


def mark_context_gathered(feature_id: str = "") -> None:
    """Record that code context (RAG/GitNexus) was gathered for a feature.

    Called by the query_rag / query_gitnexus tool handlers. Falls back to the
    current thread-local feature when no id is passed.
    """
    fid = feature_id or get_feature_id()
    if fid:
        with _lock:
            _context_gathered.add(fid)


def was_context_gathered(feature_id: str = "") -> bool:
    """Return True if RAG/GitNexus context was gathered for the feature."""
    fid = feature_id or get_feature_id()
    if not fid:
        return False
    with _lock:
        return fid in _context_gathered


def get_workspace_id() -> str:
    return getattr(_local, "workspace_id", "")


def get_feature_id() -> str:
    return getattr(_local, "feature_id", "")


def get_user_id() -> str:
    return getattr(_local, "user_id", "")


def get_org_id() -> str:
    return getattr(_local, "org_id", "")
