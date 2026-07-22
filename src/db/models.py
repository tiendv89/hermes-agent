from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.sql import func as sql_func

# Session ids are native UUID columns (migration 005). as_uuid=False keeps the
# Python side as plain strings, so existing code that treats session_id as a
# string is unaffected.
SessionUUID = UUID(as_uuid=False)


class Base(DeclarativeBase):
    pass


class Session(Base):
    __tablename__ = "sessions"

    # Core — mirrors hermes SessionDB schema
    id = Column(SessionUUID, primary_key=True)
    source = Column(String, nullable=False)
    user_id = Column(String)
    model = Column(String)
    model_config = Column(Text)
    system_prompt = Column(Text)
    parent_session_id = Column(SessionUUID, ForeignKey("sessions.id"))
    started_at = Column(Double, nullable=False)
    ended_at = Column(Double)
    end_reason = Column(String)
    message_count = Column(Integer, nullable=False, default=0)
    tool_call_count = Column(Integer, nullable=False, default=0)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    cache_read_tokens = Column(Integer, nullable=False, default=0)
    cache_write_tokens = Column(Integer, nullable=False, default=0)
    reasoning_tokens = Column(Integer, nullable=False, default=0)
    api_call_count = Column(Integer, nullable=False, default=0)
    estimated_cost_usd = Column(Double)
    actual_cost_usd = Column(Double)
    cost_status = Column(String)
    cost_source = Column(String)
    pricing_version = Column(String)
    billing_provider = Column(String)
    billing_base_url = Column(String)
    billing_mode = Column(String)
    cwd = Column(String)
    title = Column(String)
    archived = Column(Boolean, nullable=False, default=False)

    # Gateway-specific
    workspace_id = Column(String, nullable=False, default="")
    feature_id = Column(String, nullable=False, default="")
    last_active_at = Column(Double, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    extra = Column("metadata", JSONB, nullable=False, default=dict)

    # v4 team-chat: 'thread' (default) or 'channel'
    kind = Column(String, nullable=False, default="thread")

    __table_args__ = (
        Index("idx_sessions_source", "source"),
        Index("idx_sessions_started", "started_at"),
        Index("idx_sessions_user", "user_id"),
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(
        SessionUUID, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    role = Column(String, nullable=False)
    content = Column(Text)
    tool_call_id = Column(String)
    tool_calls = Column(Text)
    tool_name = Column(String)
    finish_reason = Column(String)
    reasoning = Column(Text)
    reasoning_content = Column(Text)
    reasoning_details = Column(Text)
    codex_reasoning_items = Column(Text)
    codex_message_items = Column(Text)
    token_count = Column(Integer)
    platform_message_id = Column(String)
    observed = Column(Boolean, nullable=False, default=False)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(Double, nullable=False)

    # v4 team-chat: sender X-User-Id or 'agent' sentinel; NULL for legacy rows
    author_id = Column(String)

    # m3-agent-cta: CTA suggestions attached to an assistant turn
    cta_suggestions = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))

    # agent-chat-images: storage-service image ids the user attached to this
    # message (bare ids, not URLs — resolved to a fetchable relative URL by
    # the reading router, which has the session's workspace_id in scope).
    image_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))

    # chat-file-upload: storage-service file ids the user attached to this
    # message (bare ids, not URLs — resolved to a fetchable relative URL by
    # the reading router, which has the session's workspace_id in scope).
    # Same JSONB pattern as image_ids.
    file_ids = Column(JSONB, nullable=False, server_default=text("'[]'::jsonb"))

    # chat-reply-and-thread: message-level reply/thread linkage (nullable;
    # NULL on both = plain message with no reply or thread context).
    reply_to_message_id = Column(BigInteger, ForeignKey("messages.id"), nullable=True)
    thread_root_id = Column(BigInteger, ForeignKey("messages.id"), nullable=True)

    # m3-agent-chat-essential-feature: edit/forward support.
    # edited_at: None = never edited; set to epoch seconds on content edit.
    edited_at = Column(Double, nullable=True)
    # forwarded_from_message_id: None = original; non-None = forwarded copy pointing
    # at the immediate source message.
    forwarded_from_message_id = Column(
        BigInteger, ForeignKey("messages.id"), nullable=True
    )

    __table_args__ = (
        Index("idx_messages_session", "session_id", "created_at"),
        Index("idx_messages_session_active", "session_id", "active", "created_at"),
        Index("idx_messages_author", "session_id", "author_id"),
        Index("idx_messages_thread_root", "session_id", "thread_root_id", "created_at"),
        Index("idx_messages_reply_to", "reply_to_message_id"),
    )


class SessionMember(Base):
    """Explicit membership for threads and channels (v4 team-chat)."""

    __tablename__ = "session_members"

    session_id = Column(
        SessionUUID,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    user_id = Column(String, nullable=False, primary_key=True)
    role_label = Column(String)
    added_by = Column(String, nullable=False)
    added_at = Column(Double, nullable=False)

    __table_args__ = (Index("idx_session_members_user", "user_id"),)


class SessionRead(Base):
    """Per-user "last read" cursor for a session — powers the general
    unread-message-count badge (as opposed to MessageMention's @mention-only
    count). Decoupled from SessionMember so it also covers thread owners who
    never get an explicit membership row."""

    __tablename__ = "session_reads"

    session_id = Column(
        SessionUUID,
        ForeignKey("sessions.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    user_id = Column(String, nullable=False, primary_key=True)
    last_read_message_count = Column(Integer, nullable=False, default=0)
    updated_at = Column(Double, nullable=False)

    __table_args__ = (Index("idx_session_reads_user", "user_id"),)


class MessageMention(Base):
    """Resolved @mentions within messages (v4 team-chat)."""

    __tablename__ = "message_mentions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    message_id = Column(
        BigInteger, ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    session_id = Column(
        SessionUUID, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False
    )
    mentioned_id = Column(String, nullable=False)
    mentioned_kind = Column(String, nullable=False)  # 'user' | 'agent'
    read_at = Column(Double)

    __table_args__ = (
        Index("idx_message_mentions_session", "session_id"),
        Index("idx_message_mentions_user", "session_id", "mentioned_id", "read_at"),
    )


class ModelCatalog(Base):
    """Admin-editable model identity. One row per model."""

    __tablename__ = "model_catalog"

    model_id = Column(String, primary_key=True)
    display_name = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    is_active = Column(Boolean, nullable=False, default=True)
    is_default = Column(Boolean, nullable=False, default=False)
    created_at = Column(
        DateTime(timezone=True), nullable=False, server_default=sql_func.now()
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=sql_func.now(),
        onupdate=sql_func.now(),
    )


class MessageReaction(Base):
    """Per-user emoji reaction on a message (m3-agent-chat-essential-feature).

    Toggle semantics: one row per (message_id, user_id, emoji) triple.
    The unique index enforces at-most-one row, enabling INSERT ... ON CONFLICT DO NOTHING
    for idempotent add and a plain DELETE for remove.
    """

    __tablename__ = "message_reactions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    message_id = Column(
        BigInteger,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = Column(String, nullable=False)
    emoji = Column(String, nullable=False)
    created_at = Column(Double, nullable=False)

    __table_args__ = (
        Index("idx_message_reactions_message", "message_id"),
        Index(
            "uq_message_reactions_user_emoji",
            "message_id",
            "user_id",
            "emoji",
            unique=True,
        ),
    )


class MessageSave(Base):
    """Per-user bookmark on a message (m3-agent-chat-essential-feature).

    Composite PK (message_id, user_id) makes save idempotent and unsave a cheap
    PK-lookup DELETE.
    """

    __tablename__ = "message_saves"

    message_id = Column(
        BigInteger,
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
        primary_key=True,
    )
    user_id = Column(String, nullable=False, primary_key=True)
    saved_at = Column(Double, nullable=False)

    __table_args__ = (Index("idx_message_saves_user", "user_id", "saved_at"),)
