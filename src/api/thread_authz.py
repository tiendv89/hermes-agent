"""Shared authorization check for thread-like (kind='thread') sessions.

Sessions of kind='thread' are org-public, like channels (§m3-chat-public-
session): any confirmed member of the workspace's owning org is authorized
without an explicit session_members row (see db.store.can_view_session).
Confirming that membership takes two remote calls — workflow-backend (resolve
the workspace's owning org) and user-service (confirm org membership) —
either of which can fail or come back empty. That must never be silently
treated as "not a member": a caller who is refused because the network hiccuped
gets an indistinguishable 403 from one who is genuinely unauthorized, which is
what happens if the failure/empty result is coerced into an empty org_id.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import can_view_session
from src.services.user_service_client import is_org_member
from src.services.workflow_backend_client import get_workspace_organization_id

logger = logging.getLogger(__name__)


async def authorize_thread_access(
    db: AsyncSession,
    session,
    user_id: str,
    org_id_hint: str,
) -> tuple[bool, str]:
    """Authorize *user_id* to view/post to *session*.

    Returns ``(caller_is_workspace_member, org_id)`` — org_id is the session
    workspace's resolved owning org ("" for non-thread kinds, which don't need
    it), handed back so callers needing it downstream (e.g. @mention
    resolution) don't repeat the workflow-backend round trip.

    Raises HTTPException:
      502 — the workspace-org / org-membership lookup itself failed, or
            workflow-backend returned no owning org for the session's
            workspace. Ambiguous — we cannot confirm or deny membership.
      403 — the lookup succeeded and user_id is confirmed not a member (or,
            for kind='channel'/'dm', no explicit session_members row exists).
    """
    kind_val = getattr(session, "kind", "thread") or "thread"
    ws_id = getattr(session, "workspace_id", "") or ""

    caller_is_workspace_member = False
    org_id = ""
    if kind_val == "thread":
        try:
            org_id = await get_workspace_organization_id(
                ws_id, user_id=user_id, org_id=org_id_hint
            ) or ""
            if not org_id:
                raise LookupError(
                    f"workflow-backend returned no owning org for workspace {ws_id!r}"
                )
            caller_is_workspace_member = await is_org_member(org_id, user_id)
        except Exception:
            logger.exception(
                "thread membership check unavailable for workspace %s (user=%s)",
                ws_id,
                user_id,
            )
            raise HTTPException(
                status_code=502,
                detail=(
                    "Unable to verify workspace membership right now "
                    "(workflow-backend/user-service lookup failed) — please retry."
                ),
            )

    authorized = await can_view_session(db, session, user_id, caller_is_workspace_member)
    if not authorized:
        raise HTTPException(status_code=403, detail="Not a member of this thread.")

    return caller_is_workspace_member, org_id
