"""Session lifecycle routes.

POST   /session                       — create a new session
GET    /sessions                      — list sessions for a workspace+feature
GET    /sessions/{session_id}/messages — load a session's transcript
DELETE /sessions/{session_id}          — hard-delete a single session
DELETE /sessions                       — hard-delete all of a feature's sessions
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.db import (
    create_session,
    delete_session,
    delete_sessions_for_feature,
    get_session,
    get_session_messages,
    list_sessions,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class CreateSessionRequest(BaseModel):
    # Identity is taken from the BFF-injected X-User-Id header, not the body.
    # user_id is kept (optional) only as a fallback for direct/local calls.
    user_id: str = ""
    workspace_id: str = ""
    feature_id: str = ""


class CreateSessionResponse(BaseModel):
    session_id: str


@router.post("/session", response_model=CreateSessionResponse)
async def create_session_endpoint(
    body: CreateSessionRequest,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> CreateSessionResponse:
    user_id = identity.user_id or body.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")
    session_id = await create_session(
        db,
        user_id=user_id,
        workspace_id=body.workspace_id,
        feature_id=body.feature_id,
    )
    logger.info("Created session %s for user %s", session_id, user_id)
    return CreateSessionResponse(session_id=session_id)


@router.get("/sessions")
async def list_sessions_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    feature_id: str = Query(..., description="Feature slug or ID"),
    limit: int = Query(50, ge=1, le=200, description="Max sessions to return"),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return the caller's own non-archived sessions for a workspace+feature.

    Sessions are private single-user agent chats, so the list is scoped to the
    caller (X-User-Id) — another user's sessions never appear here.
    """
    sessions = await list_sessions(
        db,
        workspace_id=workspace_id,
        feature_id=feature_id,
        user_id=identity.user_id or None,
        limit=limit,
    )
    return JSONResponse({"sessions": sessions})


@router.get("/sessions/{session_id}/messages")
async def get_session_messages_endpoint(
    session_id: str,
    _identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return the full transcript for a session, oldest-first."""
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    messages = await get_session_messages(db, session_id)
    return JSONResponse({"session_id": session_id, "messages": messages})


@router.delete("/sessions")
async def delete_all_sessions_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    feature_id: str = Query(..., description="Feature slug or ID"),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Hard-delete all of the caller's sessions for a workspace+feature."""
    deleted = await delete_sessions_for_feature(
        db,
        workspace_id=workspace_id,
        feature_id=feature_id,
        user_id=identity.user_id or None,
    )
    return JSONResponse({"deleted": deleted})


@router.delete("/sessions/{session_id}")
async def delete_session_endpoint(
    session_id: str,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Hard-delete a single session (and its messages) the caller owns."""
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    owner_id = getattr(session, "user_id", None) or ""
    if identity.user_id and owner_id and identity.user_id != owner_id:
        raise HTTPException(status_code=403, detail="Not your session.")
    await delete_session(db, session_id)
    return JSONResponse({"ok": True, "session_id": session_id})
