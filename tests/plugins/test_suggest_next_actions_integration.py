"""Integration tests for the suggest_next_actions tool (m3-agent-cta T4).

Exercises the end-to-end async path with a real database and real SSE bus:
  _persist_and_publish() → messages.cta_suggestions populated + turn.cta_suggestions event

Requires a real Postgres database. Set DATABASE_URL and run:
    DATABASE_URL=postgresql+asyncpg://user:pass@localhost/hermes_test \
        pytest -m integration tests/plugins/test_suggest_next_actions_integration.py

These tests are excluded from the default CI run (addopts = "-m 'not integration'").
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from unittest.mock import patch

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from src.db.models import Base, Message
from src.db.models import Session as DBSession
from src.db.store import get_latest_assistant_message_id, update_message_cta_suggestions
from src.realtime.bus import SessionBus


pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_DATABASE_URL = os.environ.get("DATABASE_URL", "")

_VALID_SUGGESTION = {
    "id": "sug-1",
    "title": "Review the spec",
    "category": "Review",
    "description": "Read the product spec and post your feedback.",
    "action_text": "/review spec",
    "button_label": "Review spec",
}


def _require_db() -> None:
    if not _DATABASE_URL:
        pytest.skip("DATABASE_URL not set — skipping Postgres integration test")


@pytest_asyncio.fixture
async def pg_engine():
    _require_db()
    engine = create_async_engine(_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def db_factory(pg_engine):
    """Return an async context-manager factory backed by the real Postgres engine."""
    session_factory = sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with session_factory() as session:
            yield session

    return factory


@pytest_asyncio.fixture
async def session_row(pg_engine):
    """Insert a test session + assistant message; yield (session_id, message_id); clean up after."""
    session_id = f"cta-integ-{int(time.time() * 1000)}"

    session_factory = sessionmaker(pg_engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as db:
        now = time.time()
        db.add(
            DBSession(
                id=session_id,
                source="test",
                started_at=now,
                last_active_at=now,
                is_active=True,
                archived=False,
            )
        )
        await db.flush()
        msg = Message(
            session_id=session_id,
            role="assistant",
            content="integration test message",
            created_at=now,
            observed=False,
            active=True,
        )
        db.add(msg)
        await db.flush()
        message_id = msg.id
        await db.commit()

    yield session_id, message_id

    async with session_factory() as db:
        await db.execute(
            text("DELETE FROM messages WHERE session_id = :sid"), {"sid": session_id}
        )
        await db.execute(
            text("DELETE FROM sessions WHERE id = :sid"), {"sid": session_id}
        )
        await db.commit()


async def test_persist_and_publish_populates_db_and_emits_event(session_row, db_factory):
    """_persist_and_publish writes cta_suggestions to the DB and emits turn.cta_suggestions."""
    from plugins.tools.suggest_next_actions import _persist_and_publish

    session_id, expected_message_id = session_row
    suggestions = [_VALID_SUGGESTION]

    local_bus = SessionBus()
    received: list = []

    async with local_bus.subscribe(session_id) as queue:
        with patch("src.realtime.bus.get_bus", return_value=local_bus):
            await _persist_and_publish(session_id, suggestions, db_factory)
        try:
            received.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            pass

    # Bus event received.
    assert len(received) == 1, f"Expected 1 SSE event, got {len(received)}: {received}"
    event = received[0]
    assert event["event"] == "turn.cta_suggestions"
    assert event["data"]["message_id"] == expected_message_id
    assert event["data"]["suggestions"] == suggestions

    # DB row updated.
    async with db_factory() as db:
        result = await db.execute(
            select(Message.cta_suggestions).where(Message.id == expected_message_id)
        )
        stored = result.scalar_one()
        if isinstance(stored, str):
            stored = json.loads(stored)
        assert stored == suggestions


async def test_get_latest_assistant_message_id_real_session(session_row, db_factory):
    """get_latest_assistant_message_id returns the correct id with a real AsyncSession."""
    session_id, expected_message_id = session_row

    async with db_factory() as db:
        result = await get_latest_assistant_message_id(db, session_id)

    assert result == expected_message_id


async def test_update_message_cta_suggestions_real_session(session_row, db_factory):
    """update_message_cta_suggestions persists correctly with a real AsyncSession."""
    session_id, message_id = session_row
    suggestions = [_VALID_SUGGESTION]

    async with db_factory() as db:
        await update_message_cta_suggestions(db, session_id, message_id, suggestions)

    async with db_factory() as db:
        result = await db.execute(
            select(Message.cta_suggestions).where(Message.id == message_id)
        )
        stored = result.scalar_one()
        if isinstance(stored, str):
            stored = json.loads(stored)
    assert stored == suggestions


async def test_persist_and_publish_message_id_null_when_no_assistant_message(db_factory):
    """When no assistant message exists, the event is published with message_id: null."""
    from plugins.tools.suggest_next_actions import _persist_and_publish

    session_id = f"cta-no-msg-{int(time.time() * 1000)}"
    suggestions = [_VALID_SUGGESTION]

    local_bus = SessionBus()
    received: list = []

    async with local_bus.subscribe(session_id) as queue:
        with patch("src.realtime.bus.get_bus", return_value=local_bus):
            await _persist_and_publish(session_id, suggestions, db_factory)
        try:
            received.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            pass

    assert len(received) == 1
    event = received[0]
    assert event["data"]["message_id"] is None, (
        "Expected message_id: null when no assistant message exists"
    )
    assert event["data"]["suggestions"] == suggestions
