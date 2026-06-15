"""Workspace-level team thread routes (T9).

Routes (all under ``/api/v1``, require_identity):

    POST /threads                           — create a workspace-level thread
    GET  /threads?workspace_id=<ws>         — list caller's workspace threads
    GET  /sessions?workspace_id=<ws>        — (existing, extended) now returns
                                              feature threads + workspace threads

A workspace thread is a ``kind='thread'``, ``feature_id=''`` session with explicit
membership (unlike a public channel). The full T2/T3 conversation stack
(send, SSE fan-out, @agent dispatch) applies unchanged — these routes only
handle creation and listing.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.db import (
    create_workspace_thread,
    list_workspace_threads,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateThreadRequest(BaseModel):
    workspace_id: str
    title: Optional[str] = None
    members: Optional[List[str]] = None


class ThreadResponse(BaseModel):
    thread_id: str


# ---------------------------------------------------------------------------
# POST /threads
# ---------------------------------------------------------------------------


@router.post("/threads", status_code=201)
async def create_thread_endpoint(
    body: CreateThreadRequest,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a workspace-level team thread.

    The caller is auto-joined as creator. Optional *members* list adds extra
    members at creation time. Returns 400 if workspace_id is missing.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")
    if not body.workspace_id:
        raise HTTPException(status_code=400, detail="workspace_id is required.")

    thread_id = await create_workspace_thread(
        db,
        workspace_id=body.workspace_id,
        creator_user_id=user_id,
        title=body.title,
        members=body.members,
    )

    logger.info(
        "Workspace thread created: %s (workspace=%s, creator=%s, members=%s)",
        thread_id,
        body.workspace_id,
        user_id,
        body.members,
    )
    return JSONResponse({"thread_id": thread_id}, status_code=201)


# ---------------------------------------------------------------------------
# GET /threads
# ---------------------------------------------------------------------------


@router.get("/threads")
async def list_threads_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    limit: int = Query(50, ge=1, le=200, description="Max threads to return"),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return workspace-level threads the caller owns or is a member of.

    Excludes feature threads (feature_id != '') and channels (kind='channel').
    Non-members are excluded — only own ∪ member-of threads are returned.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    threads = await list_workspace_threads(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        limit=limit,
    )
    return JSONResponse({"threads": threads})
