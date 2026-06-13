"""Session lifecycle routes.

    POST /session                       — create a new session
    GET  /sessions                      — list sessions for a workspace+feature
    GET  /sessions/{session_id}/messages — load a session's transcript
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
    _identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return non-archived sessions for a workspace+feature, newest-first."""
    sessions = await list_sessions(
        db, workspace_id=workspace_id, feature_id=feature_id, limit=limit
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
