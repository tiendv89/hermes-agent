"""Aggregate router for the workflow gateway.

The gateway's endpoints are split by concern under ``src/api/routers/``; this
module assembles them into the single ``router`` mounted at ``/api/v1`` in
``src/app.py``.

    POST /session                                — sessions
    GET  /sessions                               — sessions
    GET  /sessions/{session_id}/messages         — sessions
    GET  /models                                 — models
    POST /chat                                   — chat (streaming SSE)
    PUT  /features/{feature_id}/document         — documents (human save)
    GET  /tools                                  — tools + skills registry
    POST /features/{feature_id}/stage-transition — stages (approve/reject/reopen)
"""

from __future__ import annotations

from fastapi import APIRouter

from src.api.routers import chat, documents, models, sessions, stages, tools

router = APIRouter()
router.include_router(sessions.router)
router.include_router(models.router)
router.include_router(chat.router)
router.include_router(documents.router)
router.include_router(tools.router)
router.include_router(stages.router)

# Re-exported for callers/tests that reach for the in-flight run registry on
# this module. These are the SAME objects the chat router mutates, so adding to
# ``_active_runs`` here is observed by the handler.
from src.api.routers.chat import _active_runs, _active_runs_lock  # noqa: E402,F401

__all__ = ["router", "_active_runs", "_active_runs_lock"]
