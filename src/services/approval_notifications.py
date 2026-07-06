"""Fan-out for feature stage-approval notifications (spec/design/tasks approved).

Called from the human-facing stage-transition endpoint and the approve_feature
agent tool after a successful approve action. Fire-and-forget and best-effort:
never raises, mirroring the contract of notification_client's
schedule_notification(s) functions that it ultimately calls into.

Notifies every member of the workspace's organization (not just feature
participants) — broader than strictly necessary, but avoids silently no-op'ing
when a feature has no other session participants yet (e.g. right after the
first stage is approved, before a tech lead or reviewer has joined).
"""

from __future__ import annotations

import logging
from typing import Optional

from src.services.author_resolver import author_for
from src.services.notification_client import (
    STAGE_CATEGORY,
    build_approval_payload,
    schedule_notifications_bulk,
)
from src.services.user_service_client import list_org_members
from src.services.workflow_backend_client import get_workspace_organization_id

logger = logging.getLogger(__name__)


async def notify_stage_approved(
    workspace_id: str,
    feature_id: str,
    stage: str,
    actor_user_id: Optional[str],
    actor_org_id: Optional[str] = None,
) -> None:
    """Notify every other member of the workspace's org that `stage` was
    approved. No-op for stages that don't map to a notification category
    (e.g. "handoff"), or when the actor's identity is unknown.

    actor_org_id is the approving user's own org — passed through as the
    caller identity for the workspace-org lookup (the approver is assumed to
    belong to the same org as the workspace they're acting in).
    """
    if stage not in STAGE_CATEGORY:
        return
    if not actor_user_id:
        return

    try:
        org_id = await get_workspace_organization_id(
            workspace_id, user_id=actor_user_id, org_id=actor_org_id
        )
        if not org_id:
            return

        members = await list_org_members(org_id)
        participants = set(members)
        participants.discard(actor_user_id)
        if not participants:
            return

        actor = await author_for(workspace_id, actor_user_id)
        actor_name = actor.get("name") if actor else None

        payloads = [
            build_approval_payload(
                workspace_id=workspace_id,
                user_id=uid,
                feature_id=feature_id,
                stage=stage,
                actor_user_id=actor_user_id,
                actor_name=actor_name,
            )
            for uid in participants
        ]
        schedule_notifications_bulk(payloads)
    except Exception:
        logger.exception(
            "notify_stage_approved failed for workspace=%s feature=%s stage=%s",
            workspace_id,
            feature_id,
            stage,
        )
