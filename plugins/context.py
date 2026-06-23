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

# Features for which the agent has gathered code context (query_rag /
# query_gitnexus) during this process. Keyed by feature id (or slug). Used to
# hard-gate design-doc writes — see plugins/tools/artifacts.py. Set once context
# is gathered and never cleared, so doc revisions later in the session are not
# re-blocked.
_context_gathered: set[str] = set()


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
