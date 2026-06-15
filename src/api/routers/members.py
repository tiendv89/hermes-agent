"""Membership, read-state, and unread-count API for threads & channels.

Routes (all under ``/api/v1``, require_identity):

    GET    /threads/{session_id}/members   — list members (read-only)
    POST   /threads/{session_id}/read      — clear caller's unread mentions
    GET    /unread?workspace_id=<ws>       — caller's unread counts per session

Threads and channels are both ``Session`` rows, so these operate on session ids.
Membership is managed by joining a channel (POST /channels/{id}/join); there is
no manual add/remove here. The store layer (store_v4) backs all of this; these
are the thin HTTP handlers the FE (members panel, unread badges, mark-as-read) calls.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.db import (
    get_unread_mentions_by_session,
    list_members,
    mark_mentions_read,
)

logger = logging.getLogger(__name__)

router = APIRouter()


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
    """Clear the caller's unread @mention indicators in this thread/channel."""
    caller = identity.user_id
    if not caller:
        raise HTTPException(status_code=400, detail="Missing caller identity.")
    await mark_mentions_read(db, session_id, caller)
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
