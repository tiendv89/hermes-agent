"""Postgres session store for the workflow gateway — SQLAlchemy ORM."""

from __future__ import annotations

import json
import logging
import pathlib
import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .models import Message, MessageMention, Session, SessionMember

logger = logging.getLogger(__name__)

# migrations/ lives at the repo root (src/db/store.py -> src -> repo root).
_MIGRATIONS_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "migrations"

_CREATE_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    applied_at  DOUBLE PRECISION NOT NULL
)
"""


async def init_db(engine: AsyncEngine) -> None:
    """Run all pending SQL migrations in filename order."""
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_MIGRATIONS_TABLE))

        result = await conn.execute(text("SELECT filename FROM schema_migrations"))
        applied = {row[0] for row in result.fetchall()}

        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            if path.name in applied:
                continue
            for stmt in path.read_text(encoding="utf-8").split(";"):
                stmt = stmt.strip()
                if stmt:
                    await conn.execute(text(stmt))
            await conn.execute(
                text(
                    "INSERT INTO schema_migrations (filename, applied_at) VALUES (:f, :t)"
                ),
                {"f": path.name, "t": time.time()},
            )
            logger.info("src: applied migration %s", path.name)


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------


def _new_session_id() -> str:
    return str(uuid.uuid4())


async def create_session(
    db: AsyncSession,
    user_id: str = "",
    workspace_id: str = "",
    feature_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    now = time.time()
    session = Session(
        id=_new_session_id(),
        source="hermes-agent",
        user_id=user_id,
        workspace_id=workspace_id,
        feature_id=feature_id,
        started_at=now,
        last_active_at=now,
        extra=metadata or {},
    )
    db.add(session)
    await db.commit()
    return session.id


async def get_session(db: AsyncSession, session_id: str) -> Optional[Session]:
    result = await db.execute(select(Session).where(Session.id == session_id))
    return result.scalar_one_or_none()


async def touch_session(
    db: AsyncSession,
    session_id: str,
    user_id: Optional[str] = None,
    workspace_id: Optional[str] = None,
    feature_id: Optional[str] = None,
) -> None:
    values: Dict[str, Any] = {"last_active_at": time.time()}
    if user_id:
        values["user_id"] = user_id
    if workspace_id:
        values["workspace_id"] = workspace_id
    if feature_id:
        values["feature_id"] = feature_id
    await db.execute(update(Session).where(Session.id == session_id).values(**values))
    await db.commit()


async def update_token_counts(
    db: AsyncSession,
    session_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    reasoning_tokens: int = 0,
    api_call_count: int = 0,
    estimated_cost_usd: Optional[float] = None,
    actual_cost_usd: Optional[float] = None,
    cost_status: Optional[str] = None,
    cost_source: Optional[str] = None,
    pricing_version: Optional[str] = None,
    billing_provider: Optional[str] = None,
    billing_base_url: Optional[str] = None,
    billing_mode: Optional[str] = None,
    model: Optional[str] = None,
) -> None:
    values: Dict[str, Any] = {
        "input_tokens": Session.input_tokens + input_tokens,
        "output_tokens": Session.output_tokens + output_tokens,
        "cache_read_tokens": Session.cache_read_tokens + cache_read_tokens,
        "cache_write_tokens": Session.cache_write_tokens + cache_write_tokens,
        "reasoning_tokens": Session.reasoning_tokens + reasoning_tokens,
        "api_call_count": Session.api_call_count + api_call_count,
        "last_active_at": time.time(),
    }
    if estimated_cost_usd is not None:
        values["estimated_cost_usd"] = estimated_cost_usd
    if actual_cost_usd is not None:
        values["actual_cost_usd"] = actual_cost_usd
    if cost_status is not None:
        values["cost_status"] = cost_status
    if cost_source is not None:
        values["cost_source"] = cost_source
    if pricing_version is not None:
        values["pricing_version"] = pricing_version
    if billing_provider is not None:
        values["billing_provider"] = billing_provider
    if billing_base_url is not None:
        values["billing_base_url"] = billing_base_url
    if billing_mode is not None:
        values["billing_mode"] = billing_mode
    if model is not None:
        values["model"] = model

    await db.execute(update(Session).where(Session.id == session_id).values(**values))
    await db.commit()


# ---------------------------------------------------------------------------
# Session lifecycle / metadata updates
# ---------------------------------------------------------------------------


async def end_session(db: AsyncSession, session_id: str, end_reason: str) -> None:
    await db.execute(
        update(Session)
        .where(Session.id == session_id, Session.ended_at == None)  # noqa: E711
        .values(ended_at=time.time(), end_reason=end_reason)
    )
    await db.commit()


async def update_session_cwd(db: AsyncSession, session_id: str, cwd: str) -> None:
    await db.execute(update(Session).where(Session.id == session_id).values(cwd=cwd))
    await db.commit()


async def update_session_meta(
    db: AsyncSession,
    session_id: str,
    model_config: Optional[str],
    model: Optional[str] = None,
) -> None:
    values: Dict[str, Any] = {"model_config": model_config}
    if model is not None:
        values["model"] = model
    await db.execute(update(Session).where(Session.id == session_id).values(**values))
    await db.commit()


async def update_system_prompt(
    db: AsyncSession, session_id: str, system_prompt: str
) -> None:
    await db.execute(
        update(Session)
        .where(Session.id == session_id)
        .values(system_prompt=system_prompt)
    )
    await db.commit()


async def update_session_model(db: AsyncSession, session_id: str, model: str) -> None:
    await db.execute(
        update(Session).where(Session.id == session_id).values(model=model)
    )
    await db.commit()


async def set_session_title(db: AsyncSession, session_id: str, title: str) -> None:
    await db.execute(
        update(Session).where(Session.id == session_id).values(title=title)
    )
    await db.commit()


async def set_session_archived(
    db: AsyncSession, session_id: str, archived: bool
) -> None:
    await db.execute(
        update(Session).where(Session.id == session_id).values(archived=archived)
    )
    await db.commit()


async def _delete_sessions_by_ids(db: AsyncSession, session_ids: list[str]) -> int:
    """Hard-delete the given sessions and their child rows (messages, mentions,
    members). Children are removed explicitly in FK order so deletion works even
    where ON DELETE CASCADE isn't enforced at the DB level. Returns the count."""
    if not session_ids:
        return 0
    await db.execute(
        delete(MessageMention).where(MessageMention.session_id.in_(session_ids))
    )
    await db.execute(
        delete(SessionMember).where(SessionMember.session_id.in_(session_ids))
    )
    await db.execute(delete(Message).where(Message.session_id.in_(session_ids)))
    result = await db.execute(delete(Session).where(Session.id.in_(session_ids)))
    await db.commit()
    return result.rowcount or 0


async def delete_session(db: AsyncSession, session_id: str) -> None:
    """Hard-delete a single session and all its messages/mentions/members."""
    await _delete_sessions_by_ids(db, [session_id])


async def delete_sessions_for_feature(
    db: AsyncSession,
    workspace_id: str,
    feature_id: str,
    user_id: Optional[str] = None,
) -> int:
    """Hard-delete all of a user's non-channel sessions for a workspace+feature.

    Scoped to the caller (user_id) and excludes channels, matching list_sessions.
    Returns the number of sessions deleted.
    """
    conditions = [
        Session.workspace_id == workspace_id,
        Session.feature_id == feature_id,
        Session.kind != "channel",
    ]
    if user_id:
        conditions.append(Session.user_id == user_id)
    rows = await db.execute(select(Session.id).where(*conditions))
    ids = [r[0] for r in rows.all()]
    return await _delete_sessions_by_ids(db, ids)


async def delete_sessions_for_workspace(db: AsyncSession, workspace_id: str) -> int:
    """Hard-delete EVERY session for a workspace — all users, all features, and
    channels included. Used for service-to-service cleanup when a workspace (or
    its org) is deleted upstream. Returns the number of sessions deleted.
    """
    if not workspace_id:
        return 0
    rows = await db.execute(select(Session.id).where(Session.workspace_id == workspace_id))
    ids = [r[0] for r in rows.all()]
    return await _delete_sessions_by_ids(db, ids)


# ---------------------------------------------------------------------------
# Message CRUD
# ---------------------------------------------------------------------------


async def append_message(
    db: AsyncSession,
    session_id: str,
    role: str,
    content: Optional[str] = None,
    tool_name: Optional[str] = None,
    tool_calls: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    finish_reason: Optional[str] = None,
    reasoning: Optional[str] = None,
    reasoning_content: Optional[str] = None,
    reasoning_details: Optional[str] = None,
    codex_reasoning_items: Optional[str] = None,
    codex_message_items: Optional[str] = None,
    token_count: Optional[int] = None,
    platform_message_id: Optional[str] = None,
    observed: bool = False,
    author_id: Optional[str] = None,
) -> int:
    msg = Message(
        session_id=session_id,
        role=role,
        content=content,
        tool_name=tool_name,
        tool_calls=tool_calls,
        tool_call_id=tool_call_id,
        finish_reason=finish_reason,
        reasoning=reasoning,
        reasoning_content=reasoning_content,
        reasoning_details=reasoning_details,
        codex_reasoning_items=codex_reasoning_items,
        codex_message_items=codex_message_items,
        token_count=token_count,
        platform_message_id=platform_message_id,
        observed=observed,
        active=True,
        created_at=time.time(),
        author_id=author_id,
    )
    db.add(msg)

    # Keep session counters in sync.
    counts: Dict[str, Any] = {"message_count": Session.message_count + 1}
    if role == "tool" or (role == "assistant" and tool_calls):
        counts["tool_call_count"] = Session.tool_call_count + 1
    await db.execute(update(Session).where(Session.id == session_id).values(**counts))

    await db.commit()
    return msg.id


async def get_messages_as_conversation(
    db: AsyncSession,
    session_id: str,
) -> list[Dict[str, Any]]:
    """Return active messages in OpenAI conversation format, ordered by created_at."""
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id, Message.active == True)  # noqa: E712
        .order_by(Message.created_at)
    )
    messages = []
    for msg in result.scalars().all():
        # Coerce NULL content to "" — assistant messages that only made tool
        # calls store no text, and a null `content` is rejected by stricter
        # OpenAI-compatible providers (e.g. DeepSeek: "content should be a
        # string or a list"). Anthropic tolerates null, so this was previously
        # latent. Empty string is valid for every provider.
        entry: Dict[str, Any] = {"role": msg.role, "content": msg.content or ""}
        if msg.tool_call_id:
            entry["tool_call_id"] = msg.tool_call_id
        if msg.tool_name:
            entry["tool_name"] = msg.tool_name
        if msg.tool_calls:
            # Stored as a JSON string; the agent (and repair_message_sequence)
            # expect a parsed list of tool-call dicts. Returning the raw string
            # makes tool-call-id matching fail, which drops the historical tool
            # message, shrinks the in-place messages list, and desyncs the
            # session-DB flush cursor — silently dropping the next user turn.
            try:
                entry["tool_calls"] = json.loads(msg.tool_calls)
            except (ValueError, TypeError):
                entry["tool_calls"] = msg.tool_calls
        if msg.finish_reason:
            entry["finish_reason"] = msg.finish_reason
        if msg.reasoning:
            entry["reasoning"] = msg.reasoning
        messages.append(entry)
    return messages


async def get_session_messages(
    db: AsyncSession,
    session_id: str,
) -> list[Dict[str, Any]]:
    """Return active messages for a session in UI-friendly form, oldest-first.

    Unlike :func:`get_messages_as_conversation` (which builds OpenAI request
    context), this is shaped for rendering a chat transcript: each entry carries
    a stable ``id`` and ``tool_calls`` is parsed back into JSON when present.
    """
    result = await db.execute(
        select(Message)
        .where(Message.session_id == session_id, Message.active == True)  # noqa: E712
        .order_by(Message.created_at, Message.id)
    )
    messages = []
    for msg in result.scalars().all():
        entry: Dict[str, Any] = {
            "id": str(msg.id),
            "role": msg.role,
            "content": msg.content or "",
            "author_id": msg.author_id,
            "created_at": msg.created_at,
        }
        if msg.tool_name:
            entry["tool_name"] = msg.tool_name
        if msg.tool_call_id:
            entry["tool_call_id"] = msg.tool_call_id
        if msg.tool_calls:
            try:
                entry["tool_calls"] = json.loads(msg.tool_calls)
            except (ValueError, TypeError):
                entry["tool_calls"] = msg.tool_calls
        messages.append(entry)
    return messages


async def get_messages_since(
    db: AsyncSession,
    session_id: str,
    since_message_id: int,
) -> list[Dict[str, Any]]:
    """Return active messages with id > since_message_id, oldest-first.

    Used by the SSE stream endpoint's ``?since=`` replay to catch up a
    reconnecting client without missing events that arrived while the bus queue
    was empty (§4.3 / T3).
    """
    result = await db.execute(
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.active == True,  # noqa: E712
            Message.id > since_message_id,
        )
        .order_by(Message.created_at, Message.id)
    )
    messages = []
    for msg in result.scalars().all():
        entry: Dict[str, Any] = {
            "id": str(msg.id),
            "session_id": session_id,
            "role": msg.role,
            "content": msg.content or "",
            "author_id": msg.author_id,
            "created_at": msg.created_at,
        }
        if msg.tool_name:
            entry["tool_name"] = msg.tool_name
        messages.append(entry)
    return messages


# ---------------------------------------------------------------------------
# Session listing
# ---------------------------------------------------------------------------


async def _last_assistant_excerpt(db: AsyncSession, session_id: str) -> str:
    """Return first 120 chars of the last active assistant message in the session."""
    result = await db.execute(
        select(Message.content)
        .where(
            Message.session_id == session_id,
            Message.role == "assistant",
            Message.active == True,  # noqa: E712
            Message.content.isnot(None),
        )
        .order_by(Message.created_at.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    if row is None:
        return ""
    return row[:120]


async def list_sessions(
    db: AsyncSession,
    workspace_id: str,
    feature_id: str,
    user_id: Optional[str] = None,
    limit: int = 50,
) -> list[Dict[str, Any]]:
    """Return non-archived agent-chat sessions for a workspace+feature, newest-first.

    Excludes channels (kind='channel'), which are feature-scoped sessions surfaced
    in their own CHANNELS list — without this they'd double up under Sessions.

    When ``user_id`` is provided, only that user's own sessions are returned —
    sessions are private single-user agent chats, not shared like channels.
    """
    conditions = [
        Session.workspace_id == workspace_id,
        Session.feature_id == feature_id,
        Session.kind != "channel",
        Session.archived == False,  # noqa: E712
    ]
    if user_id:
        conditions.append(Session.user_id == user_id)

    result = await db.execute(
        select(
            Session.id,
            Session.title,
            Session.started_at,
            Session.last_active_at,
            Session.model,
        )
        .where(*conditions)
        .order_by(Session.last_active_at.desc())
        .limit(limit)
    )
    rows = result.all()
    out = []
    for row in rows:
        excerpt = await _last_assistant_excerpt(db, row.id)
        out.append(
            {
                "id": row.id,
                "title": row.title or "(untitled)",
                "started_at": row.started_at,
                "last_active_at": row.last_active_at,
                "last_message_excerpt": excerpt,
                "model": row.model,
            }
        )
    return out


async def get_latest_assistant_message_id(
    db: AsyncSession,
    session_id: str,
) -> Optional[int]:
    """Return the id of the most recently created assistant message for session_id."""
    result = await db.execute(
        select(Message.id)
        .where(Message.session_id == session_id, Message.role == "assistant")
        .order_by(Message.id.desc())
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row


async def update_message_cta_suggestions(
    db: AsyncSession,
    session_id: str,
    message_id: int,
    suggestions: list,
) -> None:
    """Persist CTA suggestions JSON onto the given assistant message row."""
    await db.execute(
        update(Message)
        .where(Message.id == message_id, Message.session_id == session_id)
        .values(cta_suggestions=json.dumps(suggestions))
    )
    await db.commit()


# ===========================================================================
# Team-chat v4 — members, mentions, channels, workspace threads
# (merged from the former store_v4.py; _new_session_id is shared from above)
# ===========================================================================
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
