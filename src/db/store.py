"""Postgres session store for the workflow gateway — SQLAlchemy ORM."""

from __future__ import annotations

import json
import logging
import pathlib
import time
import uuid
from typing import Any, Dict, List, Optional

from sqlalchemy import delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .models import Message, MessageMention, MessageReaction, ModelCatalog, Session, SessionMember, SessionRead
from src.services.author_resolver import author_for
from src.services.notification_client import (
    build_channel_message_payload,
    build_dm_payload,
    build_mention_payload,
    schedule_notifications_bulk,
)

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
            sql_no_comments = "\n".join(
                line for line in path.read_text(encoding="utf-8").splitlines()
                if not line.strip().startswith("--")
            )
            for stmt in sql_no_comments.split(";"):
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
    rows = await db.execute(
        select(Session.id).where(Session.workspace_id == workspace_id)
    )
    ids = [r[0] for r in rows.all()]
    return await _delete_sessions_by_ids(db, ids)


# ---------------------------------------------------------------------------
# Notification helpers (fire-and-forget; called from message/mention writes)
# ---------------------------------------------------------------------------


async def _emit_message_notifications(
    db: AsyncSession,
    session_id: str,
    message_id: int,
    author_id: str,
    content: str = "",
    reply_to_message_id: Optional[int] = None,
    thread_root_id: Optional[int] = None,
) -> None:
    """Look up the session kind and emit channel_message or dm notifications.

    Called after append_message commits; errors are caught so they never
    propagate to the caller. The actual HTTP call is scheduled as a
    background task (fire-and-forget via schedule_notifications_bulk).

    `thread_root_id` set means this message was posted through the thread side
    panel; `reply_to_message_id` set (with `thread_root_id` absent) means it's an
    inline quoted reply in the main transcript. Either way it's a reply, not a
    plain top-level post, so channel_message notifications for it get a "replied
    to a thread"/"replied to a message" summary instead of the plain "<name>:
    <text>" one — otherwise a reply looks identical to an ordinary channel
    message in the activity feed.
    """
    reply_kind = "thread" if thread_root_id is not None else ("message" if reply_to_message_id is not None else None)
    try:
        session = await db.get(Session, session_id)
        if session is None:
            return
        kind = getattr(session, "kind", "thread") or "thread"
        ws_id = getattr(session, "workspace_id", "") or ""
        if not ws_id:
            return

        actor = await author_for(ws_id, author_id)
        actor_name = actor.get("name") if actor else None

        if kind == "channel":
            # Notify all channel members except the message author.
            result = await db.execute(
                select(SessionMember.user_id).where(
                    SessionMember.session_id == session_id,
                    SessionMember.user_id != author_id,
                )
            )
            recipient_ids = [row[0] for row in result.all()]
            if recipient_ids:
                payloads = [
                    build_channel_message_payload(
                        workspace_id=ws_id,
                        user_id=uid,
                        message_id=message_id,
                        session_id=session_id,
                        content=content,
                        actor_user_id=author_id,
                        actor_name=actor_name,
                        feature_id=getattr(session, "feature_id", "") or None,
                        reply_kind=reply_kind,
                    )
                    for uid in recipient_ids
                ]
                schedule_notifications_bulk(payloads)

        elif kind == "dm":
            # Notify the other party in the DM session.
            result = await db.execute(
                select(SessionMember.user_id).where(
                    SessionMember.session_id == session_id,
                    SessionMember.user_id != author_id,
                )
            )
            recipient_ids = [row[0] for row in result.all()]
            if recipient_ids:
                payloads = [
                    build_dm_payload(
                        workspace_id=ws_id,
                        user_id=uid,
                        message_id=message_id,
                        session_id=session_id,
                        content=content,
                        actor_user_id=author_id,
                        actor_name=actor_name,
                    )
                    for uid in recipient_ids
                ]
                schedule_notifications_bulk(payloads)
    except Exception:
        logger.exception(
            "_emit_message_notifications failed for session=%s message=%s",
            session_id,
            message_id,
        )


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
    reply_to_message_id: Optional[int] = None,
    thread_root_id: Optional[int] = None,
    image_ids: Optional[List[str]] = None,
    forwarded_from_message_id: Optional[int] = None,
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
        reply_to_message_id=reply_to_message_id,
        thread_root_id=thread_root_id,
        # image_ids is a JSONB column — SQLAlchemy's dialect serializes a
        # native Python list on its own. Do NOT json.dumps() this first: that
        # double-encodes it into a JSON string containing JSON text, so a
        # later read gets back a plain string (not a list) and any code that
        # iterates it (e.g. building per-image URLs) walks it character by
        # character instead of element by element.
        image_ids=image_ids or [],
        forwarded_from_message_id=forwarded_from_message_id,
    )
    db.add(msg)

    # Keep session counters in sync.
    counts: Dict[str, Any] = {"message_count": Session.message_count + 1}
    if role == "tool" or (role == "assistant" and tool_calls):
        counts["tool_call_count"] = Session.tool_call_count + 1
    await db.execute(update(Session).where(Session.id == session_id).values(**counts))

    await db.flush()
    message_id = msg.id
    await db.commit()

    if role == "user" and author_id:
        # Sending implies you're caught up on this session — advance your own
        # read cursor so your own message never shows up as "unread" to you.
        await mark_session_read(db, session_id, author_id)

        # Fire-and-forget notifications for human-authored messages.
        # channel_message: broadcast to all channel members except the author.
        # dm: notify the other party in a DM session.
        if content:
            await _emit_message_notifications(
                db, session_id, message_id, author_id, content, reply_to_message_id=reply_to_message_id, thread_root_id=thread_root_id
            )

    return message_id


async def get_message(
    db: AsyncSession,
    message_id: int,
) -> Optional[Message]:
    """Return a single Message row by primary key, or None if not found."""
    result = await db.execute(select(Message).where(Message.id == message_id))
    return result.scalar_one_or_none()


async def edit_message(
    db: AsyncSession,
    message_id: int,
    content: str,
) -> None:
    """Update a message's content and stamp edited_at with the current epoch time."""
    await db.execute(
        update(Message)
        .where(Message.id == message_id)
        .values(content=content, edited_at=time.time())
    )
    await db.commit()


async def soft_delete_message(
    db: AsyncSession,
    message_id: int,
) -> None:
    """Soft-delete a message by setting active=False (idempotent)."""
    await db.execute(
        update(Message).where(Message.id == message_id).values(active=False)
    )
    await db.commit()


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
    user_id: str = "",
) -> list[Dict[str, Any]]:
    """Return messages for a session in UI-friendly form, oldest-first.

    Unlike :func:`get_messages_as_conversation` (which builds OpenAI request
    context), this is shaped for rendering a chat transcript: each entry carries
    a stable ``id`` and ``tool_calls`` is parsed back into JSON when present.

    Soft-deleted messages (active=False) are included as placeholder rows rather
    than omitted, so reply/thread linkage is preserved for ``reply_to_message_id``
    and ``QuotedParentPreview`` consumers. Their content is replaced with the
    canonical deleted-message string and a ``deleted: True`` flag is added.

    When ``user_id`` is provided, each message entry includes a ``reactions``
    list (``[{emoji, count, reactedByMe}]``) fetched in a single batch query.
    """
    result = await db.execute(
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.thread_root_id == None,  # noqa: E711
        )
        .order_by(Message.created_at, Message.id)
    )
    raw_msgs = result.scalars().all()

    # Batch-fetch reactions for all messages in one query (avoid N+1).
    msg_ids = [int(msg.id) for msg in raw_msgs]
    reactions_by_msg = await get_reactions_for_messages(db, msg_ids, user_id)

    messages = []
    for msg in raw_msgs:
        if not msg.active:
            entry: Dict[str, Any] = {
                "id": str(msg.id),
                "role": msg.role,
                "content": "This message was deleted",
                "author_id": msg.author_id,
                "created_at": msg.created_at,
                "deleted": True,
            }
            if msg.reply_to_message_id is not None:
                entry["reply_to_message_id"] = str(msg.reply_to_message_id)
        else:
            entry = {
                "id": str(msg.id),
                "role": msg.role,
                "content": msg.content or "",
                "author_id": msg.author_id,
                "created_at": msg.created_at,
            }
            if msg.edited_at is not None:
                entry["edited_at"] = msg.edited_at
            if msg.tool_name:
                entry["tool_name"] = msg.tool_name
            if msg.tool_call_id:
                entry["tool_call_id"] = msg.tool_call_id
            if msg.tool_calls:
                try:
                    entry["tool_calls"] = json.loads(msg.tool_calls)
                except (ValueError, TypeError):
                    entry["tool_calls"] = msg.tool_calls
            if msg.reply_to_message_id is not None:
                entry["reply_to_message_id"] = str(msg.reply_to_message_id)
            if msg.image_ids:
                entry["image_ids"] = msg.image_ids
            if msg.forwarded_from_message_id is not None:
                entry["forwarded_from_message_id"] = str(msg.forwarded_from_message_id)
        mid = int(msg.id)
        if mid in reactions_by_msg:
            entry["reactions"] = reactions_by_msg[mid]
        messages.append(entry)
    return messages


async def get_messages_since(
    db: AsyncSession,
    session_id: str,
    since_message_id: int,
) -> list[Dict[str, Any]]:
    """Return messages with id > since_message_id, oldest-first.

    Used by the SSE stream endpoint's ``?since=`` replay to catch up a
    reconnecting client without missing events that arrived while the bus queue
    was empty (§4.3 / T3).

    Includes thread replies (``thread_root_id`` set) alongside top-level
    messages — the frontend's ``message.created`` handler already routes a
    reply into the open thread panel (or just bumps that thread's summary
    count if the panel isn't open) and never lets it leak into the main
    transcript, so replaying them here is safe. Without this, an SSE
    reconnect mid-turn silently drops a thread reply from the live view even
    though it's correctly persisted — recoverable only by reloading.

    Soft-deleted messages are included as placeholders (same semantics as
    :func:`get_session_messages`) so the SSE catch-up path does not diverge
    from the initial-load path.
    """
    result = await db.execute(
        select(Message)
        .where(
            Message.session_id == session_id,
            Message.id > since_message_id,
        )
        .order_by(Message.created_at, Message.id)
    )
    messages = []
    for msg in result.scalars().all():
        if not msg.active:
            entry: Dict[str, Any] = {
                "id": str(msg.id),
                "session_id": session_id,
                "role": msg.role,
                "content": "This message was deleted",
                "author_id": msg.author_id,
                "created_at": msg.created_at,
                "deleted": True,
            }
            if msg.reply_to_message_id is not None:
                entry["reply_to_message_id"] = str(msg.reply_to_message_id)
            if msg.thread_root_id is not None:
                entry["thread_root_id"] = str(msg.thread_root_id)
        else:
            entry = {
                "id": str(msg.id),
                "session_id": session_id,
                "role": msg.role,
                "content": msg.content or "",
                "author_id": msg.author_id,
                "created_at": msg.created_at,
            }
            if msg.edited_at is not None:
                entry["edited_at"] = msg.edited_at
            if msg.tool_name:
                entry["tool_name"] = msg.tool_name
            if msg.reply_to_message_id is not None:
                entry["reply_to_message_id"] = str(msg.reply_to_message_id)
            if msg.thread_root_id is not None:
                entry["thread_root_id"] = str(msg.thread_root_id)
            if msg.image_ids:
                entry["image_ids"] = msg.image_ids
            if msg.forwarded_from_message_id is not None:
                entry["forwarded_from_message_id"] = str(msg.forwarded_from_message_id)
        messages.append(entry)
    return messages


async def get_thread_replies(
    db: AsyncSession,
    session_id: str,
    root_message_id: int,
    since: Optional[int] = None,
) -> list[Dict[str, Any]]:
    """Return replies belonging to the message thread rooted at root_message_id.

    Results are ordered oldest-first. When *since* is provided (a message id),
    only replies with id > since are returned (SSE catch-up use-case).

    Soft-deleted replies are included as placeholders (same semantics as
    :func:`get_session_messages`) so thread UI can show the deleted indicator.
    """
    conditions = [
        Message.session_id == session_id,
        Message.thread_root_id == root_message_id,
    ]
    if since is not None:
        conditions.append(Message.id > since)

    result = await db.execute(
        select(Message)
        .where(*conditions)
        .order_by(Message.created_at, Message.id)
    )
    messages = []
    for msg in result.scalars().all():
        if not msg.active:
            entry: Dict[str, Any] = {
                "id": str(msg.id),
                "session_id": session_id,
                "role": msg.role,
                "content": "This message was deleted",
                "author_id": msg.author_id,
                "created_at": msg.created_at,
                "thread_root_id": str(root_message_id),
                "deleted": True,
            }
            if msg.reply_to_message_id is not None:
                entry["reply_to_message_id"] = str(msg.reply_to_message_id)
        else:
            entry = {
                "id": str(msg.id),
                "session_id": session_id,
                "role": msg.role,
                "content": msg.content or "",
                "author_id": msg.author_id,
                "created_at": msg.created_at,
                "thread_root_id": str(root_message_id),
            }
            if msg.edited_at is not None:
                entry["edited_at"] = msg.edited_at
            if msg.reply_to_message_id is not None:
                entry["reply_to_message_id"] = str(msg.reply_to_message_id)
            if msg.tool_name:
                entry["tool_name"] = msg.tool_name
            if msg.image_ids:
                entry["image_ids"] = msg.image_ids
        messages.append(entry)
    return messages


async def get_thread_reply_summaries(
    db: AsyncSession,
    session_id: str,
    root_message_ids: List[int],
) -> Dict[int, Dict[str, Any]]:
    """Return reply count and recent repliers for each root message id.

    Executes a single grouped query (no N+1). Returns a dict keyed by
    root_message_id; absent keys mean zero replies. ``recent_repliers`` lists
    the distinct author_id values of the three most recent repliers.
    """
    if not root_message_ids:
        return {}

    result = await db.execute(
        select(
            Message.thread_root_id,
            func.count(Message.id).label("reply_count"),
        )
        .where(
            Message.session_id == session_id,
            Message.thread_root_id.in_(root_message_ids),
            Message.active == True,  # noqa: E712
        )
        .group_by(Message.thread_root_id)
    )
    counts: Dict[int, int] = {row.thread_root_id: row.reply_count for row in result.all()}

    # Fetch the most recent replies per thread root to extract recent repliers.
    # One query with a window function alternative; we use a simpler approach:
    # fetch all reply author_ids ordered by id desc and pick the first 3 distinct
    # per root in Python (avoids complex lateral joins for portability).
    replier_result = await db.execute(
        select(Message.thread_root_id, Message.author_id)
        .where(
            Message.session_id == session_id,
            Message.thread_root_id.in_(root_message_ids),
            Message.active == True,  # noqa: E712
            Message.author_id != None,  # noqa: E711
        )
        .order_by(Message.thread_root_id, Message.id.desc())
    )

    recent_repliers: Dict[int, list] = {}
    for thread_root_id, author_id in replier_result.all():
        seen = recent_repliers.setdefault(thread_root_id, [])
        if author_id not in seen and len(seen) < 3:
            seen.append(author_id)

    summaries: Dict[int, Dict[str, Any]] = {}
    for root_id in root_message_ids:
        if root_id in counts:
            summaries[root_id] = {
                "reply_count": counts[root_id],
                "recent_repliers": recent_repliers.get(root_id, []),
            }
    return summaries


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


# ---------------------------------------------------------------------------
# Reaction store
# ---------------------------------------------------------------------------


async def get_reactions_for_messages(
    db: AsyncSession,
    message_ids: List[int],
    user_id: str = "",
) -> Dict[int, List[Dict[str, Any]]]:
    """Return aggregated reactions for a set of messages in one query (no N+1).

    Returns a dict keyed by message_id. Each value is a list of
    ``{emoji, count, reactedByMe, userIds}`` dicts ordered by first-reaction time.
    ``userIds`` is the raw list of reactor user ids — callers resolve display names
    (e.g. for a "who reacted" tooltip) via user-service and replace it with ``users``.
    Messages with no reactions are absent from the dict.
    """
    if not message_ids:
        return {}

    result = await db.execute(
        select(
            MessageReaction.message_id,
            MessageReaction.emoji,
            func.count(MessageReaction.id).label("cnt"),
            func.bool_or(MessageReaction.user_id == user_id).label("reacted_by_me"),
            func.array_agg(MessageReaction.user_id).label("user_ids"),
        )
        .where(MessageReaction.message_id.in_(message_ids))
        .group_by(MessageReaction.message_id, MessageReaction.emoji)
        .order_by(MessageReaction.message_id, func.min(MessageReaction.created_at))
    )

    reactions_by_msg: Dict[int, List[Dict[str, Any]]] = {}
    for row in result.all():
        mid = int(row.message_id)
        reactions_by_msg.setdefault(mid, []).append(
            {
                "emoji": row.emoji,
                "count": int(row.cnt),
                "reactedByMe": bool(row.reacted_by_me),
                "userIds": list(row.user_ids),
            }
        )
    return reactions_by_msg


async def toggle_message_reaction(
    db: AsyncSession,
    message_id: int,
    user_id: str,
    emoji: str,
) -> List[Dict[str, Any]]:
    """Toggle a per-user emoji reaction on a message.

    Inserts a new ``MessageReaction`` row if one doesn't exist for
    ``(message_id, user_id, emoji)``; deletes it if one does exist.

    Returns the updated aggregate reaction list for the message:
    ``[{emoji, count, reactedByMe}]``.
    """
    stmt = (
        pg_insert(MessageReaction)
        .values(
            message_id=message_id,
            user_id=user_id,
            emoji=emoji,
            created_at=time.time(),
        )
        .on_conflict_do_nothing(index_elements=["message_id", "user_id", "emoji"])
    )
    result = await db.execute(stmt)

    if result.rowcount == 0:
        # Row already existed — toggle means delete
        await db.execute(
            delete(MessageReaction).where(
                MessageReaction.message_id == message_id,
                MessageReaction.user_id == user_id,
                MessageReaction.emoji == emoji,
            )
        )

    await db.commit()

    per_msg = await get_reactions_for_messages(db, [message_id], user_id)
    return per_msg.get(message_id, [])


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
    content: str = "",
    author_id: Optional[str] = None,
) -> None:
    """Persist resolved mentions for a message.

    Each entry in ``mentions`` must have ``mentioned_id`` and
    ``mentioned_kind`` ('user' | 'agent').

    author_id is the sender's user_id, used to populate actor_user_id in
    the mention notification payloads. content is the raw message text, used
    to build a Slack-style preview in the notification summary.
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

        # Fire-and-forget mention notifications for human recipients.
        user_mentions = [m for m in mentions if m["mentioned_kind"] == "user"]
        if user_mentions:
            session = await db.get(Session, session_id)
            ws_id = (getattr(session, "workspace_id", "") or "") if session else ""
            if ws_id:
                actor = await author_for(ws_id, author_id) if author_id else None
                actor_name = actor.get("name") if actor else None
                payloads = [
                    build_mention_payload(
                        workspace_id=ws_id,
                        user_id=m["mentioned_id"],
                        message_id=message_id,
                        session_id=session_id,
                        content=content,
                        actor_user_id=author_id,
                        actor_name=actor_name,
                        feature_id=(getattr(session, "feature_id", "") or None) if session else None,
                    )
                    for m in user_mentions
                ]
                schedule_notifications_bulk(payloads)


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


async def mark_session_read(
    db: AsyncSession,
    session_id: str,
    user_id: str,
) -> None:
    """Advance user_id's read cursor for session_id to the current message count.

    Decoupled from mark_mentions_read (mentions have their own read state) —
    callers that want both call each explicitly.
    """
    session = await db.get(Session, session_id)
    if session is None:
        return
    now = time.time()
    existing = await db.get(SessionRead, (session_id, user_id))
    if existing is not None:
        existing.last_read_message_count = session.message_count
        existing.updated_at = now
    else:
        db.add(
            SessionRead(
                session_id=session_id,
                user_id=user_id,
                last_read_message_count=session.message_count,
                updated_at=now,
            )
        )
    await db.commit()


async def get_unread_message_counts_by_session(
    db: AsyncSession,
    workspace_id: str,
    user_id: str,
) -> Dict[str, int]:
    """Return unread message counts keyed by session id, for every channel/DM/
    thread user_id participates in. Unlike mention counts, this reflects ANY
    new message since the user's last-read cursor, not just @mentions."""
    result = await db.execute(
        select(Session.id, Session.message_count, SessionRead.last_read_message_count)
        .outerjoin(
            SessionRead,
            (SessionRead.session_id == Session.id) & (SessionRead.user_id == user_id),
        )
        .where(
            Session.workspace_id == workspace_id,
            Session.archived == False,  # noqa: E712
            Session.kind.in_(("channel", "dm", "thread")),
            or_(
                Session.user_id == user_id,
                Session.id.in_(
                    select(SessionMember.session_id).where(
                        SessionMember.user_id == user_id
                    )
                ),
            ),
        )
    )
    counts: Dict[str, int] = {}
    for session_id, message_count, last_read in result.all():
        unread = (message_count or 0) - (last_read or 0)
        if unread > 0:
            counts[session_id] = unread
    return counts


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


# ---------------------------------------------------------------------------
# Direct Message (DM) store (agent-general-chat G2)
# ---------------------------------------------------------------------------


async def create_dm(
    db: AsyncSession,
    workspace_id: str,
    member_a: str,
    member_b: str,
) -> str:
    """Resolve-or-create a DM session for the unordered pair (member_a, member_b).

    Idempotent: if a DM session already exists for this pair within the
    workspace, the existing session id is returned without creating a new row.
    DM sessions have kind='dm' and feature_id=''.
    """
    # Look up an existing DM session where both members are present.
    # We query sessions the caller is a member of, then check the other member.
    existing = await db.execute(
        select(Session.id)
        .where(
            Session.workspace_id == workspace_id,
            Session.kind == "dm",
            Session.archived == False,  # noqa: E712
            Session.id.in_(
                select(SessionMember.session_id).where(
                    SessionMember.user_id == member_a
                )
            ),
            Session.id.in_(
                select(SessionMember.session_id).where(
                    SessionMember.user_id == member_b
                )
            ),
        )
        .limit(1)
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        return row

    now = time.time()
    session = Session(
        id=_new_session_id(),
        source="hermes-agent",
        user_id=member_a,
        workspace_id=workspace_id,
        feature_id="",
        kind="dm",
        started_at=now,
        last_active_at=now,
        extra={},
    )
    db.add(session)
    await db.flush()

    for uid in (member_a, member_b):
        db.add(
            SessionMember(
                session_id=session.id,
                user_id=uid,
                added_by=member_a,
                added_at=now,
            )
        )
    await db.commit()
    return session.id


async def list_dms(
    db: AsyncSession,
    workspace_id: str,
    user_id: str,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Return non-archived DM sessions the user is a member of, newest-first.

    Each entry includes ``other_member_id`` (the DM peer's user id, resolved
    from session_members) — display name/avatar are enriched by the caller
    via user-service (see ``src/api/routers/dms.py``).
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
            Session.kind == "dm",
            Session.id.in_(
                select(SessionMember.session_id).where(SessionMember.user_id == user_id)
            ),
        )
        .order_by(Session.last_active_at.desc())
        .limit(limit)
    )
    rows = result.all()
    session_ids = [row.id for row in rows]

    other_member_by_session: Dict[str, str] = {}
    if session_ids:
        member_result = await db.execute(
            select(SessionMember.session_id, SessionMember.user_id).where(
                SessionMember.session_id.in_(session_ids),
                SessionMember.user_id != user_id,
            )
        )
        for session_id, other_user_id in member_result.all():
            other_member_by_session[session_id] = other_user_id

    return [
        {
            "id": row.id,
            "title": row.title,
            "feature_id": row.feature_id,
            "started_at": row.started_at,
            "last_active_at": row.last_active_at,
            "model": row.model,
            "kind": row.kind,
            "other_member_id": other_member_by_session.get(row.id, ""),
        }
        for row in rows
    ]


# ---------------------------------------------------------------------------
# Model catalog CRUD
# ---------------------------------------------------------------------------


async def list_catalog_models(db: AsyncSession) -> List[Dict[str, Any]]:
    """Return all model_catalog rows ordered by display_name."""
    result = await db.execute(select(ModelCatalog).order_by(ModelCatalog.display_name))
    return [_catalog_row(m) for m in result.scalars().all()]


async def list_active_catalog_models(db: AsyncSession) -> List[Dict[str, Any]]:
    """Return only active model_catalog rows ordered by display_name."""
    result = await db.execute(
        select(ModelCatalog)
        .where(ModelCatalog.is_active == True)  # noqa: E712
        .order_by(ModelCatalog.display_name)
    )
    return [_catalog_row(m) for m in result.scalars().all()]


async def get_catalog_model(db: AsyncSession, model_id: str) -> Optional[ModelCatalog]:
    """Return a single ModelCatalog row or None."""
    result = await db.execute(
        select(ModelCatalog).where(ModelCatalog.model_id == model_id)
    )
    return result.scalar_one_or_none()


async def get_default_catalog_model(db: AsyncSession) -> Optional[ModelCatalog]:
    """Return the row with is_default=True (at most one, enforced by the unique index)."""
    result = await db.execute(
        select(ModelCatalog).where(
            ModelCatalog.is_default == True,  # noqa: E712
            ModelCatalog.is_active == True,  # noqa: E712
        )
    )
    return result.scalar_one_or_none()


async def create_catalog_model(
    db: AsyncSession,
    model_id: str,
    display_name: str,
    provider: str,
    is_active: bool = True,
    is_default: bool = False,
) -> ModelCatalog:
    """Insert a new model_catalog row. Raises IntegrityError on duplicate model_id."""
    row = ModelCatalog(
        model_id=model_id,
        display_name=display_name,
        provider=provider,
        is_active=is_active,
        is_default=is_default,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def update_catalog_model(
    db: AsyncSession,
    model_id: str,
    *,
    display_name: Optional[str] = None,
    is_active: Optional[bool] = None,
    is_default: Optional[bool] = None,
) -> Optional[ModelCatalog]:
    """Patch a model_catalog row.

    Setting is_default=True clears any previous default in the same transaction
    (the unique partial index on is_default WHERE is_default also enforces this
    at the DB level, but we handle it explicitly for a clean user error).

    Raises ValueError if caller tries to deactivate the current default model
    (the admin must reassign the default first).
    """
    row = await get_catalog_model(db, model_id)
    if row is None:
        return None

    # Guard: cannot deactivate the current default.
    if is_active is False and row.is_default:
        raise ValueError(
            "Cannot deactivate the current default model. "
            "Reassign the default to another model first."
        )

    # If setting this model as the new default, clear any existing default first.
    if is_default is True:
        await db.execute(
            update(ModelCatalog)
            .where(ModelCatalog.is_default == True)  # noqa: E712
            .values(is_default=False)
        )

    if display_name is not None:
        row.display_name = display_name
    if is_active is not None:
        row.is_active = is_active
    if is_default is not None:
        row.is_default = is_default

    await db.commit()
    await db.refresh(row)
    return row


def _catalog_row(m: ModelCatalog) -> Dict[str, Any]:
    return {
        "model_id": m.model_id,
        "display_name": m.display_name,
        "provider": m.provider,
        "is_active": m.is_active,
        "is_default": m.is_default,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
