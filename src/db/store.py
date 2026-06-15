"""Postgres session store for the workflow gateway — SQLAlchemy ORM."""

from __future__ import annotations

import json
import logging
import pathlib
import secrets
import time
from typing import Any, Dict, Optional

from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from .models import Message, Session

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
    return "sess_" + secrets.token_hex(16)


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
    limit: int = 50,
) -> list[Dict[str, Any]]:
    """Return non-archived sessions for a workspace+feature, newest-first."""
    result = await db.execute(
        select(
            Session.id,
            Session.title,
            Session.started_at,
            Session.last_active_at,
            Session.model,
        )
        .where(
            Session.workspace_id == workspace_id,
            Session.feature_id == feature_id,
            Session.archived == False,  # noqa: E712
        )
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
