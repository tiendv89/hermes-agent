"""v4 team-chat store functions — members, mentions, channels.

These are additive and live alongside the original store.py to keep the
diff minimal and the existing session/message CRUD untouched.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from .models import MessageMention, Session, SessionMember

# ---------------------------------------------------------------------------
# Member store
# ---------------------------------------------------------------------------

AGENT_SENTINEL = "agent"


async def add_member(
    db: AsyncSession,
    session_id: str,
    user_id: str,
    added_by: str,
    role_label: Optional[str] = None,
) -> None:
    """Add user_id to session_id's member set (idempotent — upsert on conflict)."""
    existing = await db.get(SessionMember, (session_id, user_id))
    if existing is not None:
        return
    db.add(
        SessionMember(
            session_id=session_id,
            user_id=user_id,
            role_label=role_label,
            added_by=added_by,
            added_at=time.time(),
        )
    )
    await db.commit()


async def remove_member(
    db: AsyncSession,
    session_id: str,
    user_id: str,
) -> None:
    """Remove user_id from session_id's member set (no-op if not a member)."""
    await db.execute(
        delete(SessionMember).where(
            SessionMember.session_id == session_id,
            SessionMember.user_id == user_id,
        )
    )
    await db.commit()


async def list_members(
    db: AsyncSession,
    session_id: str,
) -> List[Dict[str, Any]]:
    """Return all human members of a session, ordered by added_at."""
    result = await db.execute(
        select(SessionMember)
        .where(SessionMember.session_id == session_id)
        .order_by(SessionMember.added_at)
    )
    return [
        {
            "user_id": m.user_id,
            "role_label": m.role_label,
            "added_by": m.added_by,
            "added_at": m.added_at,
        }
        for m in result.scalars().all()
    ]


async def is_member(
    db: AsyncSession,
    session_id: str,
    user_id: str,
) -> bool:
    """True if user_id is an explicit member of session_id."""
    row = await db.get(SessionMember, (session_id, user_id))
    return row is not None


async def list_member_sessions(
    db: AsyncSession,
    workspace_id: str,
    user_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return non-archived sessions the user owns OR is a member of (G7).

    Excludes channels (kind='channel') — those are listed via list_channels().
    Returns sessions ordered by last_active_at DESC.
    """
    result = await db.execute(
        select(
            Session.id,
            Session.title,
            Session.feature_id,
            Session.started_at,
            Session.last_active_at,
            Session.model,
            Session.kind,
        )
        .where(
            Session.workspace_id == workspace_id,
            Session.archived == False,  # noqa: E712
            Session.kind == "thread",
            or_(
                Session.user_id == user_id,
                Session.id.in_(
                    select(SessionMember.session_id).where(
                        SessionMember.user_id == user_id
                    )
                ),
            ),
        )
        .order_by(Session.last_active_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": row.id,
            "title": row.title or "(untitled)",
            "feature_id": row.feature_id,
            "started_at": row.started_at,
            "last_active_at": row.last_active_at,
            "model": row.model,
            "kind": row.kind,
        }
        for row in result.all()
    ]


# ---------------------------------------------------------------------------
# Mention store
# ---------------------------------------------------------------------------


async def persist_mentions(
    db: AsyncSession,
    message_id: int,
    session_id: str,
    mentions: List[Dict[str, str]],
) -> None:
    """Persist resolved mentions for a message.

    Each entry in ``mentions`` must have ``mentioned_id`` and
    ``mentioned_kind`` ('user' | 'agent').
    """
    for m in mentions:
        db.add(
            MessageMention(
                message_id=message_id,
                session_id=session_id,
                mentioned_id=m["mentioned_id"],
                mentioned_kind=m["mentioned_kind"],
            )
        )
    if mentions:
        await db.commit()


async def resolve_mentions(
    db: AsyncSession,
    message_id: int,
) -> List[Dict[str, Any]]:
    """Return all resolved mentions for a message."""
    result = await db.execute(
        select(MessageMention).where(MessageMention.message_id == message_id)
    )
    return [
        {
            "id": m.id,
            "message_id": m.message_id,
            "session_id": m.session_id,
            "mentioned_id": m.mentioned_id,
            "mentioned_kind": m.mentioned_kind,
            "read_at": m.read_at,
        }
        for m in result.scalars().all()
    ]


async def get_unread_mention_count(
    db: AsyncSession,
    session_id: str,
    user_id: str,
) -> int:
    """Count unread mentions (read_at IS NULL) for user_id in session_id."""
    result = await db.execute(
        select(func.count(MessageMention.id)).where(
            MessageMention.session_id == session_id,
            MessageMention.mentioned_id == user_id,
            MessageMention.mentioned_kind == "user",
            MessageMention.read_at == None,  # noqa: E711
        )
    )
    return result.scalar_one() or 0


async def mark_mentions_read(
    db: AsyncSession,
    session_id: str,
    user_id: str,
) -> None:
    """Clear unread mention indicators for user_id in session_id."""
    now = time.time()
    await db.execute(
        update(MessageMention)
        .where(
            MessageMention.session_id == session_id,
            MessageMention.mentioned_id == user_id,
            MessageMention.mentioned_kind == "user",
            MessageMention.read_at == None,  # noqa: E711
        )
        .values(read_at=now)
    )
    await db.commit()


async def get_unread_mentions_by_session(
    db: AsyncSession,
    workspace_id: str,
    user_id: str,
) -> Dict[str, int]:
    """Return unread mention counts for user_id keyed by session id, scoped to a
    workspace. Used by GET /unread to render per-channel/thread badges."""
    result = await db.execute(
        select(MessageMention.session_id, func.count(MessageMention.id))
        .join(Session, Session.id == MessageMention.session_id)
        .where(
            Session.workspace_id == workspace_id,
            MessageMention.mentioned_id == user_id,
            MessageMention.mentioned_kind == "user",
            MessageMention.read_at == None,  # noqa: E711
        )
        .group_by(MessageMention.session_id)
    )
    return {row[0]: int(row[1]) for row in result.all()}


# ---------------------------------------------------------------------------
# Channel store
# ---------------------------------------------------------------------------


def _new_session_id() -> str:
    return "sess_" + secrets.token_hex(16)


async def create_channel(
    db: AsyncSession,
    workspace_id: str,
    name: str,
    creator_user_id: str,
    feature_id: str = "",
    description: Optional[str] = None,
) -> str:
    """Create a new public channel (kind='channel' session) scoped to a feature.

    The creator is auto-joined as the first member.
    Raises sqlalchemy.exc.IntegrityError if the name already exists for the same
    (workspace, feature) pair.
    """
    now = time.time()
    metadata: Dict[str, Any] = {}
    if description:
        metadata["description"] = description

    session = Session(
        id=_new_session_id(),
        source="hermes-agent",
        user_id=creator_user_id,
        workspace_id=workspace_id,
        feature_id=feature_id,
        title=name,
        kind="channel",
        started_at=now,
        last_active_at=now,
        extra=metadata,
    )
    db.add(session)
    await db.flush()  # populate session.id before adding member

    db.add(
        SessionMember(
            session_id=session.id,
            user_id=creator_user_id,
            added_by=creator_user_id,
            added_at=now,
        )
    )
    await db.commit()
    return session.id


async def list_channels(
    db: AsyncSession,
    workspace_id: str,
    feature_id: Optional[str] = None,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return non-archived channels for a workspace, newest-first.

    When ``feature_id`` is provided, only channels for that feature are
    returned (channels are feature-scoped); otherwise all workspace channels.
    """
    conditions = [
        Session.workspace_id == workspace_id,
        Session.kind == "channel",
        Session.archived == False,  # noqa: E712
    ]
    if feature_id is not None:
        conditions.append(Session.feature_id == feature_id)

    result = await db.execute(
        select(
            Session.id,
            Session.title,
            Session.user_id,
            Session.feature_id,
            Session.started_at,
            Session.last_active_at,
            Session.extra,
        )
        .where(*conditions)
        .order_by(Session.started_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": row.id,
            "name": row.title or "(unnamed)",
            "creator_user_id": row.user_id,
            "feature_id": row.feature_id,
            "started_at": row.started_at,
            "last_active_at": row.last_active_at,
            "description": (row.extra or {}).get("description"),
        }
        for row in result.all()
    ]


async def get_channel(
    db: AsyncSession,
    channel_id: str,
) -> Optional[Session]:
    """Return the Session row for channel_id if it is a non-archived channel."""
    result = await db.execute(
        select(Session).where(
            Session.id == channel_id,
            Session.kind == "channel",
            Session.archived == False,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()


async def hard_delete_channel(
    db: AsyncSession,
    channel_id: str,
) -> bool:
    """Hard-delete a channel session and all its messages (cascade).

    Returns True if the channel existed and was deleted, False if not found.
    """
    session = await get_channel(db, channel_id)
    if session is None:
        return False
    await db.delete(session)
    await db.commit()
    return True


# ---------------------------------------------------------------------------
# Workspace thread store (T9)
# ---------------------------------------------------------------------------


async def create_workspace_thread(
    db: AsyncSession,
    workspace_id: str,
    creator_user_id: str,
    title: Optional[str] = None,
    members: Optional[List[str]] = None,
) -> str:
    """Create a workspace-level team thread (kind='thread', feature_id='').

    The creator is auto-joined as the first member. Any additional user IDs in
    *members* are also added. Returns the new session id.
    """
    now = time.time()
    session = Session(
        id=_new_session_id(),
        source="hermes-agent",
        user_id=creator_user_id,
        workspace_id=workspace_id,
        feature_id="",
        title=title or None,
        kind="thread",
        started_at=now,
        last_active_at=now,
        extra={},
    )
    db.add(session)
    await db.flush()

    # Auto-join creator
    db.add(
        SessionMember(
            session_id=session.id,
            user_id=creator_user_id,
            added_by=creator_user_id,
            added_at=now,
        )
    )

    # Add any explicitly requested initial members (skip duplicates)
    if members:
        for uid in members:
            if uid == creator_user_id:
                continue
            db.add(
                SessionMember(
                    session_id=session.id,
                    user_id=uid,
                    added_by=creator_user_id,
                    added_at=now,
                )
            )

    await db.commit()
    return session.id


async def list_workspace_threads(
    db: AsyncSession,
    workspace_id: str,
    user_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return workspace-level threads the user owns or is a member of.

    Workspace threads are kind='thread' rows with feature_id=''.
    Non-members are excluded (own ∪ member-of filter).
    """
    result = await db.execute(
        select(
            Session.id,
            Session.title,
            Session.feature_id,
            Session.started_at,
            Session.last_active_at,
            Session.model,
            Session.kind,
        )
        .where(
            Session.workspace_id == workspace_id,
            Session.archived == False,  # noqa: E712
            Session.kind == "thread",
            Session.feature_id == "",
            or_(
                Session.user_id == user_id,
                Session.id.in_(
                    select(SessionMember.session_id).where(
                        SessionMember.user_id == user_id
                    )
                ),
            ),
        )
        .order_by(Session.last_active_at.desc())
        .limit(limit)
    )
    return [
        {
            "id": row.id,
            "title": row.title or "(untitled)",
            "feature_id": row.feature_id,
            "started_at": row.started_at,
            "last_active_at": row.last_active_at,
            "model": row.model,
            "kind": row.kind,
        }
        for row in result.all()
    ]
