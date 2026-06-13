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
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_by_session: dict[str, tuple[str, str]] = {}
_local = threading.local()


def set_context(session_id: str, workspace_id: str, feature_id: str) -> None:
    """Record the workspace/feature IDs for a session (and the current thread)."""
    with _lock:
        _by_session[session_id] = (workspace_id, feature_id)
    _local.workspace_id = workspace_id
    _local.feature_id = feature_id
    logger.info(
        "workflow context set: session=%s workspace_id=%r feature_id=%r",
        session_id, workspace_id, feature_id,
    )


def clear_context(session_id: str) -> None:
    """Drop the stored context for a session once its turn completes."""
    with _lock:
        _by_session.pop(session_id, None)


def get_context_for_session(session_id: str) -> tuple[str, str]:
    """Return (workspace_id, feature_id) for a session, falling back to the thread-local."""
    if session_id:
        with _lock:
            found = _by_session.get(session_id)
        if found is not None:
            return found
    return get_workspace_id(), get_feature_id()


def get_workspace_id() -> str:
    return getattr(_local, "workspace_id", "")


def get_feature_id() -> str:
    return getattr(_local, "feature_id", "")
