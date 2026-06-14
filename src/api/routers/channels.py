"""Channels API — public, workspace-scoped named conversations.

Routes (all under ``/api/v1``, require_identity):

    GET    /channels?workspace_id=<ws>       — list non-archived channels
    POST   /channels                         — create (open to any member)
    DELETE /channels/{id}                    — hard-delete (admin-gated via user-service)
    POST   /channels/{id}/join               — join (any member)

A channel is a ``kind='channel'`` Session row (§3.4 / T1 data model).
On deletion a ``channel.deleted`` event is published to the in-process bus
so live SSE subscribers (T3) can update their UI without a full page reload.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.db import (
    add_member,
    get_channel,
    hard_delete_channel,
    is_member,
    list_channels,
    create_channel,
)
from src.realtime.bus import get_bus
from src.services.user_service_client import UserServiceError, is_workspace_admin

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateChannelRequest(BaseModel):
    workspace_id: str
    name: str
    description: Optional[str] = None


class ChannelResponse(BaseModel):
    id: str
    name: str
    creator_user_id: str
    started_at: float
    last_active_at: float
    description: Optional[str] = None


# ---------------------------------------------------------------------------
# GET /channels
# ---------------------------------------------------------------------------


@router.get("/channels")
async def list_channels_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    limit: int = Query(100, ge=1, le=500, description="Max channels to return"),
    _identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return all non-archived channels for a workspace, newest-first."""
    channels = await list_channels(db, workspace_id=workspace_id, limit=limit)
    return JSONResponse({"channels": channels})


# ---------------------------------------------------------------------------
# POST /channels
# ---------------------------------------------------------------------------


@router.post("/channels", status_code=201)
async def create_channel_endpoint(
    body: CreateChannelRequest,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a public channel. Open to any workspace member — no role check.

    The creator is automatically joined as the first member.
    Returns 409 if a channel with that name already exists in the workspace.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")
    if not body.workspace_id:
        raise HTTPException(status_code=400, detail="workspace_id is required.")
    if not body.name or not body.name.strip():
        raise HTTPException(status_code=400, detail="Channel name is required.")

    try:
        channel_id = await create_channel(
            db,
            workspace_id=body.workspace_id,
            name=body.name.strip(),
            creator_user_id=user_id,
            description=body.description,
        )
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"A channel named '{body.name}' already exists in this workspace.",
        )

    logger.info(
        "Channel created: %s (name=%r, workspace=%s, creator=%s)",
        channel_id,
        body.name,
        body.workspace_id,
        user_id,
    )
    return JSONResponse({"channel_id": channel_id}, status_code=201)


# ---------------------------------------------------------------------------
# DELETE /channels/{id}
# ---------------------------------------------------------------------------


@router.delete("/channels/{channel_id}", status_code=204)
async def delete_channel_endpoint(
    channel_id: str,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Hard-delete a channel. Admin-gated: caller must be a workspace admin.

    Fetches the channel to resolve workspace_id, then verifies the caller's
    role via user-service (§3.6). On success, the channel session row and all
    its messages (cascade FK) are deleted, and a ``channel.deleted`` event is
    published to the in-process bus.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    channel = await get_channel(db, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found.")

    # Admin gate (§3.6 / T5): verify caller is workspace admin/owner.
    try:
        admin = await is_workspace_admin(channel.workspace_id, user_id)
    except UserServiceError as exc:
        logger.error("user-service error during admin check: %s", exc)
        raise HTTPException(
            status_code=502,
            detail="Could not verify caller role — user-service unavailable.",
        )

    if not admin:
        raise HTTPException(
            status_code=403,
            detail="Only workspace admins may delete channels.",
        )

    deleted = await hard_delete_channel(db, channel_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Channel not found.")

    # Publish channel.deleted so live SSE subscribers (T3) know to drop it.
    get_bus().publish(
        channel_id, {"event": "channel.deleted", "channel_id": channel_id}
    )

    logger.info(
        "Channel deleted: %s (workspace=%s, by=%s)",
        channel_id,
        channel.workspace_id,
        user_id,
    )
    return JSONResponse(None, status_code=204)


# ---------------------------------------------------------------------------
# POST /channels/{id}/join
# ---------------------------------------------------------------------------


@router.post("/channels/{channel_id}/join", status_code=200)
async def join_channel_endpoint(
    channel_id: str,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Join a channel. Any workspace member may join.

    Idempotent: re-joining an already-joined channel returns 200 without error.
    Returns 404 if the channel does not exist.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    channel = await get_channel(db, channel_id)
    if channel is None:
        raise HTTPException(status_code=404, detail="Channel not found.")

    already_member = await is_member(db, channel_id, user_id)
    await add_member(db, channel_id, user_id, added_by=user_id)

    logger.info(
        "User %s joined channel %s (already_member=%s)",
        user_id,
        channel_id,
        already_member,
    )
    return JSONResponse({"channel_id": channel_id, "user_id": user_id, "joined": True})
