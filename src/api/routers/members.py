"""Membership, read-state, and unread-count API for threads & channels.

Routes (all under ``/api/v1``, require_identity):

    GET    /threads/{session_id}/members    — list members (read-only)
    POST   /threads/{session_id}/read       — clear caller's unread mentions + advance read cursor
    GET    /unread?workspace_id=<ws>        — caller's unread @mention counts per session
    GET    /unread-messages?workspace_id=<ws> — caller's unread message counts per session (any message, not just mentions)
    GET    /notifications/stream            — persistent SSE subscription for real-time toasts

Threads and channels are both ``Session`` rows, so these operate on session ids.
Membership is managed by joining a channel (POST /channels/{id}/join); there is
no manual add/remove here. The store layer (store_v4) backs all of this; these
are the thin HTTP handlers the FE (members panel, unread badges, mark-as-read) calls.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.db import (
    get_unread_mentions_by_session,
    get_unread_message_counts_by_session,
    list_members,
    mark_mentions_read,
    mark_session_read,
)
from src.realtime.user_bus import get_user_bus

logger = logging.getLogger(__name__)

router = APIRouter()

# How long to wait for a bus event before emitting a keepalive comment.
_KEEPALIVE_SECONDS = 20.0


def _sse_frame(event: str, data: dict) -> str:
    """Render a named SSE frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


@router.get("/threads/{session_id}/members")
async def list_thread_members_endpoint(
    session_id: str,
    _identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return the human members of a thread/channel.

    Display names aren't resolved here — the store holds only the membership
    rows; the FE falls back to the user id when ``display_name`` is null.
    """
    rows = await list_members(db, session_id)
    members = [
        {
            "user_id": r["user_id"],
            "display_name": None,
            "role_label": r.get("role_label"),
            "added_at": r.get("added_at"),
        }
        for r in rows
    ]
    return JSONResponse({"members": members})


@router.post("/threads/{session_id}/read", status_code=204)
async def mark_thread_read_endpoint(
    session_id: str,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Clear the caller's unread @mention indicators AND advance their general
    read cursor for this thread/channel (opening it means you've seen both)."""
    caller = identity.user_id
    if not caller:
        raise HTTPException(status_code=400, detail="Missing caller identity.")
    await mark_mentions_read(db, session_id, caller)
    await mark_session_read(db, session_id, caller)
    return Response(status_code=204)


@router.get("/unread")
async def get_unread_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return the caller's unread @mention counts, per session and aggregate."""
    caller = identity.user_id
    if not caller:
        raise HTTPException(status_code=400, detail="Missing caller identity.")
    per_session = await get_unread_mentions_by_session(db, workspace_id, caller)
    return JSONResponse({"total": sum(per_session.values()), "perSession": per_session})


@router.get("/unread-messages")
async def get_unread_messages_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return the caller's unread message counts (any message, not just
    @mentions), per session and aggregate. Powers the general channel/DM
    unread badges in the chat sidebar and Feature IDE's channel list."""
    caller = identity.user_id
    if not caller:
        raise HTTPException(status_code=400, detail="Missing caller identity.")
    per_session = await get_unread_message_counts_by_session(db, workspace_id, caller)
    return JSONResponse({"total": sum(per_session.values()), "perSession": per_session})


@router.get("/notifications/stream")
async def stream_notifications(
    identity: Identity = Depends(require_identity),
) -> StreamingResponse:
    """Open a persistent SSE subscription for the caller's own notification
    events (mentions, channel messages, DMs, stage approvals). Emits a
    ``notification.created`` frame per event, carrying the same payload shape
    notification-service persists — the FE renders a toast and refreshes the
    Activity feed/unread badges on receipt.

    Global per-user topic (not scoped to a single thread), unlike
    ``GET /threads/{id}/stream``.
    """
    caller = identity.user_id
    if not caller:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    bus = get_user_bus()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    bus.subscribe_raw(caller, queue)

    async def event_generator():
        try:
            while True:
                try:
                    bus_event = await asyncio.wait_for(queue.get(), _KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                event_name = bus_event.get("event", "")
                data = bus_event.get("data", {})
                if not event_name:
                    continue

                yield _sse_frame(event_name, data)
                await asyncio.sleep(0)
        finally:
            bus.unsubscribe_raw(caller, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
