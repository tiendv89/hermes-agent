"""Aggregate router for the workflow gateway.

The gateway's endpoints are split by concern under ``src/api/routers/``; this
module assembles them into the single ``router`` mounted at ``/api/v1`` in
``src/app.py``.

    POST /session                                — sessions
    GET  /sessions                               — sessions
    GET  /sessions/{session_id}/messages         — sessions
    GET  /models                                 — models
    POST /chat                                   — chat (streaming SSE, legacy)
    POST /threads/{id}/messages                  — send service (v4 team-chat)
    GET  /threads/{id}/stream                    — SSE fan-out subscription (v4)
    POST /threads/{id}/typing                    — ephemeral typing indicator (v4)
    POST /threads/{id}/cancel                    — cancel in-progress agent turn
    POST /threads                                — create workspace-level thread (T9)
    GET  /threads                                — list caller's workspace threads (T9)
    PUT  /features/{feature_id}/document         — documents (human save)
    GET  /tools                                  — tools + skills registry
    POST /features/{feature_id}/stage-transition — stages (approve/reject/reopen)
    GET  /channels                               — channels (team chat)
    POST /channels                               — channels
    DELETE /channels/{id}                        — channels
    POST /channels/{id}/join                     — channels
"""

from __future__ import annotations

from fastapi import APIRouter

from src.api.routers import (
    channels,
    chat,
    documents,
    members,
    messages,
    models,
    sessions,
    stages,
    stream,
    threads,
    tools,
)

router = APIRouter()
router.include_router(sessions.router)
router.include_router(models.router)
router.include_router(chat.router)
router.include_router(messages.router)
router.include_router(stream.router)
router.include_router(documents.router)
router.include_router(tools.router)
router.include_router(stages.router)
router.include_router(channels.router)
router.include_router(threads.router)
router.include_router(members.router)

# Re-exported for callers/tests that reach for the in-flight run registry.
# Both the legacy /chat handler and the new send service share this state via
# src.api.agent_dispatch, so we re-export from there.
from src.api.agent_dispatch import _active_runs, _active_runs_lock  # noqa: E402,F401

__all__ = ["router", "_active_runs", "_active_runs_lock"]
