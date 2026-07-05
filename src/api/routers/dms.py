"""Direct Message (DM) routes — agent-general-chat G2.

Routes (all under ``/api/v1``, require_identity):

    POST /dms           — resolve-or-create a 1:1 DM session with another member
    GET  /dms           — list the caller's DM sessions for a workspace

A DM session is a ``kind='dm'``, ``feature_id=''`` session with exactly two
human ``session_members`` rows. ``create_dm`` is idempotent — calling it twice
with the same pair returns the existing session.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.db import create_dm, list_dms
from src.services.user_service_client import list_org_members, list_users_by_ids
from src.services.workflow_db_client import get_workspace_organization_id

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateDMRequest(BaseModel):
    workspace_id: str
    other_member_id: str


# ---------------------------------------------------------------------------
# POST /dms
# ---------------------------------------------------------------------------


@router.post("/dms", status_code=201)
async def create_dm_endpoint(
    body: CreateDMRequest,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Resolve-or-create a DM session between the caller and another member.

    Returns 400 if workspace_id or other_member_id is missing, or if the
    other member is the caller themselves. Returns 404 when USER_SERVICE_URL
    is set and other_member_id is not a member of the caller's org (skipped in
    dev mode when USER_SERVICE_URL is unset, or when the workspace's org can't
    be resolved).
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")
    if not body.workspace_id:
        raise HTTPException(status_code=400, detail="workspace_id is required.")
    if not body.other_member_id:
        raise HTTPException(status_code=400, detail="other_member_id is required.")
    if body.other_member_id == user_id:
        raise HTTPException(status_code=400, detail="Cannot create a DM with yourself.")

    # Validate that other_member_id is a real org member (when user-service is
    # available and the workspace's org could be resolved — permissive otherwise).
    organization_id = await get_workspace_organization_id(body.workspace_id)
    if organization_id:
        members = await list_org_members(organization_id)
        if members and body.other_member_id not in members:
            raise HTTPException(
                status_code=404,
                detail=f"Member '{body.other_member_id}' not found in organization.",
            )

    session_id = await create_dm(
        db,
        workspace_id=body.workspace_id,
        member_a=user_id,
        member_b=body.other_member_id,
    )

    logger.info(
        "DM resolved: %s (workspace=%s, caller=%s, other=%s)",
        session_id,
        body.workspace_id,
        user_id,
        body.other_member_id,
    )
    return JSONResponse({"session_id": session_id}, status_code=201)


# ---------------------------------------------------------------------------
# GET /dms
# ---------------------------------------------------------------------------


@router.get("/dms")
async def list_dms_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    limit: int = Query(50, ge=1, le=200, description="Max DMs to return"),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return the caller's DM sessions for the workspace, newest-first."""
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    dms = await list_dms(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        limit=limit,
    )

    # Enrich with the peer's display name/avatar (id-based, org-independent).
    other_ids = [dm["other_member_id"] for dm in dms if dm.get("other_member_id")]
    users = await list_users_by_ids(other_ids) if other_ids else {}
    for dm in dms:
        info = users.get(dm.get("other_member_id", "")) or {}
        dm["other_member_name"] = info.get("display_name") or info.get("email")
        dm["other_member_avatar_url"] = info.get("avatar_url")

    return JSONResponse({"dms": dms})
