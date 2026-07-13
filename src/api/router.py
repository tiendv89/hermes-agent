"""Aggregate router for the workflow gateway.

The gateway's endpoints are split by concern under ``src/api/routers/``; this
module assembles them into the single ``router`` mounted at ``/api/v1`` in
``src/app.py``.

    POST /session                                          — sessions
    GET  /sessions                                         — sessions
    GET  /sessions/{session_id}/messages                   — sessions
    GET  /models                                           — models
    GET  /admin/models                                     — admin model catalog (list)
    POST /admin/models                                     — admin model catalog (create)
    PATCH /admin/models/{id}                               — admin model catalog (update)
    POST /chat                                             — chat (streaming SSE, legacy)
    POST /threads/{id}/messages                            — send service (v4 team-chat)
    GET  /threads/{id}/stream                              — SSE fan-out subscription (v4)
    POST /threads/{id}/typing                              — ephemeral typing indicator (v4)
    POST /threads/{id}/cancel                              — cancel in-progress agent turn
    POST /threads                                          — create workspace-level thread (T9)
    GET  /threads                                          — list caller's workspace threads (T9)
    POST /threads/{id}/messages/{msg_id}/replies           — post thread reply (chat-reply-and-thread)
    GET  /threads/{id}/messages/{msg_id}/replies           — get thread replies (chat-reply-and-thread)
    PUT  /features/{feature_id}/document                   — documents (human save)
    GET  /tools                                            — tools + skills registry
    POST /features/{feature_id}/stage-transition           — stages (approve/reject/reopen)
    GET  /channels                                         — channels (team chat)
    POST /channels                                         — channels
    DELETE /channels/{id}                                  — channels
    POST /channels/{id}/join                               — channels
    POST /dms                                              — create/resolve DM session (agent-general-chat)
    GET  /dms                                              — list caller's DMs (agent-general-chat)
"""

from __future__ import annotations

from fastapi import APIRouter

from src.api.routers import (
    admin_models,
    channels,
    chat,
    dms,
    documents,
    members,
    message_saves,
    message_threads,
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
router.include_router(admin_models.router)
router.include_router(chat.router)
router.include_router(messages.router)
router.include_router(message_saves.router)
router.include_router(message_threads.router)
router.include_router(stream.router)
router.include_router(documents.router)
router.include_router(tools.router)
router.include_router(stages.router)
router.include_router(channels.router)
router.include_router(threads.router)
router.include_router(members.router)
router.include_router(dms.router)

# Re-exported for callers/tests that reach for the in-flight run registry.
# Both the legacy /chat handler and the new send service share this state via
# src.api.agent_dispatch, so we re-export from there.
from src.api.agent_dispatch import _active_runs, _active_runs_lock  # noqa: E402,F401

__all__ = ["router", "_active_runs", "_active_runs_lock"]
