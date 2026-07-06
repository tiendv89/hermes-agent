"""Fan-out for feature stage-approval notifications (spec/design/tasks approved).

Called from the human-facing stage-transition endpoint after a successful
approve action. Fire-and-forget and best-effort: never raises, mirroring the
contract of notification_client's schedule_notification(s) functions that it
ultimately calls into.
"""

from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import async_sessionmaker

from src.db.store import get_feature_participants
from src.services.author_resolver import author_for
from src.services.notification_client import (
    STAGE_CATEGORY,
    build_approval_payload,
    schedule_notifications_bulk,
)

logger = logging.getLogger(__name__)


async def notify_stage_approved(
    session_factory: async_sessionmaker,
    workspace_id: str,
    feature_id: str,
    stage: str,
    actor_user_id: Optional[str],
) -> None:
    """Notify every feature participant except the approver that `stage` was
    approved. No-op for stages that don't map to a notification category
    (e.g. "handoff"), or when the actor's identity is unknown.
    """
    if stage not in STAGE_CATEGORY:
        return
    if not actor_user_id:
        return

    try:
        async with session_factory() as db:
            participants = await get_feature_participants(db, workspace_id, feature_id)
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
