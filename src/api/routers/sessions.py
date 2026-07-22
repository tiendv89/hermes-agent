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
from src.api.identity import Identity, require_identity, require_service_token
from src.api.routers.messages import _image_urls_for, _file_urls_for
from src.db import (
    create_session,
    delete_session,
    delete_sessions_for_feature,
    delete_sessions_for_workspace,
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
    # Optional client tag (e.g. "coding-ide") so a client whose sessions all
    # share one source can later list just its own via GET /sessions?source=...
    # without mixing in every other client's sessions for the same
    # workspace+feature. Defaults to store.create_session's own default.
    source: str = ""


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
    kwargs = {"source": body.source} if body.source else {}
    session_id = await create_session(
        db,
        user_id=user_id,
        workspace_id=body.workspace_id,
        feature_id=body.feature_id,
        **kwargs,
    )
    logger.info("Created session %s for user %s", session_id, user_id)
    return CreateSessionResponse(session_id=session_id)


@router.get("/sessions")
async def list_sessions_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    feature_id: str = Query(..., description="Feature slug or ID"),
    limit: int = Query(50, ge=1, le=200, description="Max sessions to return"),
    source: str = Query("", description="Restrict to sessions created with this source tag"),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return non-archived sessions for a workspace+feature.

    Ordinary (web) sessions are org-public, like channels — every workspace
    member sees every session, regardless of who started it. The IDE's own
    sessions (source="coding-ide") are the exception: those stay scoped to
    the caller (X-User-Id), since an IDE session is one developer's local
    coding session, not a shared team thread — see store.list_sessions.
    """
    sessions = await list_sessions(
        db,
        workspace_id=workspace_id,
        feature_id=feature_id,
        user_id=identity.user_id or None,
        limit=limit,
        source=source or None,
    )
    return JSONResponse({"sessions": sessions})


@router.get("/sessions/{session_id}/messages")
async def get_session_messages_endpoint(
    session_id: str,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return the full transcript for a session, oldest-first.

    Ordinary sessions are org-public (any workspace member can open any
    session's transcript, matching list_sessions' org-public policy), but a
    "coding-ide" session is one developer's local coding session, not a
    shared thread — listing already hides other users' IDE sessions (see
    store.list_sessions), and this guards the same rule against someone
    fetching a transcript directly by a known/guessed session_id.
    """
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    owner_id = getattr(session, "user_id", None) or ""
    session_source = getattr(session, "source", None) or ""
    if session_source == "coding-ide" and identity.user_id and owner_id and identity.user_id != owner_id:
        raise HTTPException(status_code=403, detail="Not your session.")
    messages = await get_session_messages(db, session_id, user_id=identity.user_id)
    workspace_id = getattr(session, "workspace_id", "") or ""
    for m in messages:
        image_ids = m.pop("image_ids", None)
        if image_ids:
            m["image_urls"] = _image_urls_for(workspace_id, image_ids)
        file_ids = m.pop("file_ids", None)
        if file_ids:
            m["file_urls"] = _file_urls_for(workspace_id, file_ids)
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


@router.delete("/internal/workspaces/{workspace_id}/sessions", dependencies=[Depends(require_service_token)])
async def delete_workspace_sessions_endpoint(
    workspace_id: str,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Service-to-service: hard-delete EVERY session for a workspace (all users +
    channels). Called by workflow-backend when a workspace (or its org) is deleted.
    Service-token auth only — no per-user scoping."""
    deleted = await delete_sessions_for_workspace(db, workspace_id)
    return JSONResponse({"deleted": deleted})
