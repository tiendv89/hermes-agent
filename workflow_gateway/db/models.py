from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, Column, Double, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class Session(Base):
    __tablename__ = "sessions"

    # Core — mirrors hermes SessionDB schema
    id                 = Column(String, primary_key=True)
    source             = Column(String, nullable=False)
    user_id            = Column(String)
    model              = Column(String)
    model_config       = Column(Text)
    system_prompt      = Column(Text)
    parent_session_id  = Column(String, ForeignKey("sessions.id"))
    started_at         = Column(Double, nullable=False)
    ended_at           = Column(Double)
    end_reason         = Column(String)
    message_count      = Column(Integer, nullable=False, default=0)
    tool_call_count    = Column(Integer, nullable=False, default=0)
    input_tokens       = Column(Integer, nullable=False, default=0)
    output_tokens      = Column(Integer, nullable=False, default=0)
    cache_read_tokens  = Column(Integer, nullable=False, default=0)
    cache_write_tokens = Column(Integer, nullable=False, default=0)
    reasoning_tokens   = Column(Integer, nullable=False, default=0)
    api_call_count     = Column(Integer, nullable=False, default=0)
    estimated_cost_usd = Column(Double)
    actual_cost_usd    = Column(Double)
    cost_status        = Column(String)
    cost_source        = Column(String)
    pricing_version    = Column(String)
    billing_provider   = Column(String)
    billing_base_url   = Column(String)
    billing_mode       = Column(String)
    cwd                = Column(String)
    title              = Column(String)
    archived           = Column(Boolean, nullable=False, default=False)

    # Gateway-specific
    workspace_id   = Column(String, nullable=False, default="")
    feature_id     = Column(String, nullable=False, default="")
    last_active_at = Column(Double, nullable=False)
    is_active      = Column(Boolean, nullable=False, default=True)
    extra          = Column("metadata", JSONB, nullable=False, default=dict)

    __table_args__ = (
        Index("idx_sessions_source",  "source"),
        Index("idx_sessions_started", "started_at"),
        Index("idx_sessions_user",    "user_id"),
    )


class Message(Base):
    __tablename__ = "messages"

    id                   = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id           = Column(String, ForeignKey("sessions.id", ondelete="CASCADE"), nullable=False)
    role                 = Column(String, nullable=False)
    content              = Column(Text)
    tool_call_id         = Column(String)
    tool_calls           = Column(Text)
    tool_name            = Column(String)
    finish_reason        = Column(String)
    reasoning            = Column(Text)
    reasoning_content    = Column(Text)
    reasoning_details    = Column(Text)
    codex_reasoning_items = Column(Text)
    codex_message_items  = Column(Text)
    token_count          = Column(Integer)
    platform_message_id  = Column(String)
    observed             = Column(Boolean, nullable=False, default=False)
    active               = Column(Boolean, nullable=False, default=True)
    created_at           = Column(Double, nullable=False)

    __table_args__ = (
        Index("idx_messages_session",        "session_id", "created_at"),
        Index("idx_messages_session_active", "session_id", "active", "created_at"),
    )
