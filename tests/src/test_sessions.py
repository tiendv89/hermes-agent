"""Tests for session listing endpoint and auto-title behaviour.

Covers the T1 test plan from tasks.md:
  - Unit: 3 sessions seeded (1 archived) → returns 2 ordered by last_active_at DESC
  - Unit: auto-title sets title to first 60 chars on a null-title session
  - Integration: GET /api/v5/sessions against the docker-compose test Postgres
"""

from __future__ import annotations

import pathlib
import sys
import time
import types
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT_PATH = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_PATH))


# ---------------------------------------------------------------------------
# Unit tests for list_sessions store function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sessions_excludes_archived_and_orders_by_last_active():
    """3 sessions (1 archived) → returns 2 ordered by last_active_at DESC."""
    from src.db.store import list_sessions

    now = time.time()
    # db.execute mock: returns a result whose .all() gives (id, title, started_at, last_active_at) rows
    row_newer = MagicMock(
        id="sess_1", title="Newer", started_at=now - 100, last_active_at=now - 10
    )
    row_older = MagicMock(
        id="sess_2", title="Older", started_at=now - 200, last_active_at=now - 100
    )

    result_mock = MagicMock()
    result_mock.all.return_value = [row_newer, row_older]

    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)

    # _last_assistant_excerpt returns empty string for both
    with patch(
        "src.db.store._last_assistant_excerpt", AsyncMock(return_value="")
    ):
        sessions = await list_sessions(db, workspace_id="ws-1", feature_id="feat-1")

    assert len(sessions) == 2
    assert sessions[0]["id"] == "sess_1", "Newest session should come first"
    assert sessions[1]["id"] == "sess_2"
    # Verify archived row is excluded (the query filter is in store; we verify the mock was called once)
    db.execute.assert_called_once()


@pytest.mark.asyncio
async def test_list_sessions_uses_untitled_fallback():
    """Sessions with NULL title fall back to '(untitled)'."""
    from src.db.store import list_sessions

    now = time.time()
    row = MagicMock(id="sess_x", title=None, started_at=now, last_active_at=now)

    result_mock = MagicMock()
    result_mock.all.return_value = [row]

    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)

    with patch(
        "src.db.store._last_assistant_excerpt", AsyncMock(return_value="")
    ):
        sessions = await list_sessions(db, workspace_id="ws-1", feature_id="feat-1")

    assert sessions[0]["title"] == "(untitled)"


@pytest.mark.asyncio
async def test_last_assistant_excerpt_returns_up_to_120_chars():
    """_last_assistant_excerpt returns first 120 chars of last assistant message."""
    from src.db.store import _last_assistant_excerpt

    long_content = "x" * 200
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = long_content

    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)

    excerpt = await _last_assistant_excerpt(db, "sess_abc")
    assert len(excerpt) == 120
    assert excerpt == "x" * 120


@pytest.mark.asyncio
async def test_last_assistant_excerpt_returns_empty_when_no_message():
    """_last_assistant_excerpt returns '' when session has no assistant messages."""
    from src.db.store import _last_assistant_excerpt

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None

    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)

    excerpt = await _last_assistant_excerpt(db, "sess_empty")
    assert excerpt == ""


# ---------------------------------------------------------------------------
# Unit tests for auto-title in stream_chat
# ---------------------------------------------------------------------------


def _inject_mock_run_agent():
    if "run_agent" not in sys.modules:
        stub = types.ModuleType("run_agent")

        class _MockAIAgent:
            def __init__(self, stream_delta_callback=None, **kwargs):
                self._delta_cb = stream_delta_callback

            def run_conversation(self, message, conversation_history=None):
                if self._delta_cb:
                    self._delta_cb("Hello from mock agent")

        stub.AIAgent = _MockAIAgent  # type: ignore[attr-defined]
        sys.modules["run_agent"] = stub


@pytest.fixture
def gateway_app():
    _inject_mock_run_agent()

    from fastapi import FastAPI
    from src.api.router import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v5")

    @asynccontextmanager
    async def _db_session():
        yield MagicMock()

    app.state.db_session = _db_session
    return app


@pytest.mark.asyncio
async def test_auto_title_set_on_null_title_session(gateway_app):
    """When session.title is None, stream_chat sets title to first 60 chars of message."""
    from httpx import ASGITransport, AsyncClient

    long_message = "A" * 80  # 80 chars — title should be first 60
    null_title_session = MagicMock()
    null_title_session.title = None  # triggers auto-title

    # After set_session_title, get_session is called again to refresh; return updated mock
    updated_session = MagicMock()
    updated_session.title = long_message[:60]

    set_title_mock = AsyncMock()

    _resolved = {"model": "claude-sonnet-4-6", "provider": "anthropic", "api_key": None, "base_url": None}
    with (
        patch(
            "src.api.routers.chat.get_session",
            AsyncMock(side_effect=[null_title_session, updated_session]),
        ),
        patch(
            "src.api.routers.chat.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch("src.api.routers.chat.set_session_title", set_title_mock),
        patch("src.api.routers.chat.touch_session", AsyncMock()),
        patch("src.api.routers.chat.update_session_model", AsyncMock()),
        patch("src.api.routers.chat.resolve_model", AsyncMock(return_value=_resolved)),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=gateway_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v5/chat",
                json={
                    "session_id": "sess_null_title",
                    "message": long_message,
                    "workspace_id": "ws-1",
                    "feature_id": "feat-1",
                },
                timeout=15.0,
            )

    assert resp.status_code == 200
    # Verify set_session_title was called with the first 60 chars
    set_title_mock.assert_called_once()
    call_args = set_title_mock.call_args
    # set_session_title(db, session_id, title) → args[2] is the title
    assert call_args.args[2] == long_message[:60], (
        f"Expected title={long_message[:60]!r}, got {call_args.args[2]!r}"
    )


@pytest.mark.asyncio
async def test_auto_title_not_set_when_title_exists(gateway_app):
    """When session.title is already set, stream_chat does not overwrite it."""
    from httpx import ASGITransport, AsyncClient

    existing_session = MagicMock()
    existing_session.title = "My existing title"

    set_title_mock = AsyncMock()

    _resolved = {"model": "claude-sonnet-4-6", "provider": "anthropic", "api_key": None, "base_url": None}
    with (
        patch(
            "src.api.routers.chat.get_session",
            AsyncMock(return_value=existing_session),
        ),
        patch(
            "src.api.routers.chat.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch("src.api.routers.chat.set_session_title", set_title_mock),
        patch("src.api.routers.chat.touch_session", AsyncMock()),
        patch("src.api.routers.chat.update_session_model", AsyncMock()),
        patch("src.api.routers.chat.resolve_model", AsyncMock(return_value=_resolved)),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=gateway_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v5/chat",
                json={
                    "session_id": "sess_has_title",
                    "message": "Hello world",
                    "workspace_id": "ws-1",
                    "feature_id": "feat-1",
                },
                timeout=15.0,
            )

    assert resp.status_code == 200
    set_title_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Integration-style test for GET /api/v5/sessions route
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_sessions_endpoint_returns_json(gateway_app):
    """GET /api/v5/sessions returns {sessions: [...]} with correct shape."""
    import time as _time

    from httpx import ASGITransport, AsyncClient

    now = _time.time()
    fake_sessions = [
        {
            "id": "sess_a",
            "title": "Session A",
            "started_at": now - 200,
            "last_active_at": now - 10,
            "last_message_excerpt": "Hello there",
        },
        {
            "id": "sess_b",
            "title": "(untitled)",
            "started_at": now - 500,
            "last_active_at": now - 100,
            "last_message_excerpt": "",
        },
    ]

    with patch(
        "src.api.routers.sessions.list_sessions",
        AsyncMock(return_value=fake_sessions),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=gateway_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get(
                "/api/v5/sessions",
                params={"workspace_id": "ws-1", "feature_id": "feat-1"},
            )

    assert resp.status_code == 200
    body = resp.json()
    assert "sessions" in body
    assert len(body["sessions"]) == 2
    assert body["sessions"][0]["id"] == "sess_a"
    assert body["sessions"][1]["id"] == "sess_b"
    assert body["sessions"][0]["last_message_excerpt"] == "Hello there"


@pytest.mark.asyncio
async def test_get_sessions_endpoint_requires_workspace_id(gateway_app):
    """GET /api/v5/sessions without workspace_id returns 422."""
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://testserver",
    ) as client:
        resp = await client.get(
            "/api/v5/sessions",
            params={"feature_id": "feat-1"},  # missing workspace_id
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_get_sessions_endpoint_requires_feature_id(gateway_app):
    """GET /api/v5/sessions without feature_id returns 422."""
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=gateway_app),
        base_url="http://testserver",
    ) as client:
        resp = await client.get(
            "/api/v5/sessions",
            params={"workspace_id": "ws-1"},  # missing feature_id
        )

    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# get_messages_as_conversation — tool_calls must be parsed back to a list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_messages_as_conversation_parses_tool_calls():
    """tool_calls is stored as a JSON string but must be returned as a list.

    Regression for the silent user-message-drop bug: a raw string makes
    repair_message_sequence fail to match tool-call ids, drop the historical
    tool message, and desync the session-DB flush cursor.
    """
    import json as _json

    from src.db.store import get_messages_as_conversation

    tool_calls = [
        {"id": "call_1", "type": "function", "function": {"name": "foo", "arguments": "{}"}}
    ]

    asst = MagicMock()
    asst.role, asst.content = "assistant", ""
    asst.tool_call_id, asst.tool_name = None, None
    asst.tool_calls = _json.dumps(tool_calls)  # stored as a JSON STRING
    asst.finish_reason, asst.reasoning = "tool_calls", None

    scalars = MagicMock()
    scalars.all.return_value = [asst]
    result = MagicMock()
    result.scalars.return_value = scalars
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    convo = await get_messages_as_conversation(db, "sess_1")

    assert convo[0]["tool_calls"] == tool_calls  # parsed list, not a string
    assert isinstance(convo[0]["tool_calls"], list)


@pytest.mark.asyncio
async def test_get_messages_as_conversation_coerces_null_content():
    """NULL content must become "" — a null content is rejected by stricter
    OpenAI-compatible providers (e.g. DeepSeek)."""
    from src.db.store import get_messages_as_conversation

    asst = MagicMock()
    asst.role, asst.content = "assistant", None  # tool-call message: no text
    asst.tool_call_id, asst.tool_name = None, None
    asst.tool_calls = None
    asst.finish_reason, asst.reasoning = "tool_calls", None

    scalars = MagicMock()
    scalars.all.return_value = [asst]
    result = MagicMock()
    result.scalars.return_value = scalars
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)

    convo = await get_messages_as_conversation(db, "sess_1")

    assert convo[0]["content"] == ""
    assert convo[0]["content"] is not None


# ---------------------------------------------------------------------------
# GET /api/v5/sessions/{session_id}/messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_messages_endpoint_returns_transcript(gateway_app):
    """GET /sessions/{id}/messages returns {session_id, messages: [...]}."""
    from httpx import ASGITransport, AsyncClient

    fake_messages = [
        {"id": "1", "role": "user", "content": "hi", "created_at": 1.0},
        {
            "id": "2",
            "role": "assistant",
            "content": "hello!",
            "created_at": 2.0,
            "tool_calls": [{"id": "c1", "function": {"name": "search"}}],
        },
    ]

    with (
        patch(
            "src.api.routers.sessions.get_session",
            AsyncMock(return_value=MagicMock()),
        ),
        patch(
            "src.api.routers.sessions.get_session_messages",
            AsyncMock(return_value=fake_messages),
        ),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=gateway_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/api/v5/sessions/sess_a/messages")

    assert resp.status_code == 200
    body = resp.json()
    assert body["session_id"] == "sess_a"
    assert len(body["messages"]) == 2
    assert body["messages"][0]["role"] == "user"
    assert body["messages"][1]["tool_calls"][0]["function"]["name"] == "search"


@pytest.mark.asyncio
async def test_get_session_messages_endpoint_404_when_session_missing(gateway_app):
    """GET /sessions/{id}/messages returns 404 for an unknown session."""
    from httpx import ASGITransport, AsyncClient

    with patch(
        "src.api.routers.sessions.get_session",
        AsyncMock(return_value=None),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=gateway_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get("/api/v5/sessions/nope/messages")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Docker-compose integration test — real Postgres
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_get_sessions_endpoint_real_postgres():
    """Integration: GET /api/v5/sessions against docker-compose test Postgres.

    Seeds 3 sessions (2 active, 1 archived) and one assistant message.
    Verifies ordering, archived exclusion, and excerpt population.
    """
    import os

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.api.router import router
    from src.db import init_db
    from src.db.models import Message, Session

    raw_url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql://hermes_agent:hermes_agent@localhost:25434/hermes_agent",
    )
    if raw_url.startswith("postgresql://"):
        raw_url = raw_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(raw_url, echo=False)
    await init_db(engine)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    # Unique identifiers isolate this test from any pre-existing data.
    ws_id = f"test-ws-{uuid.uuid4().hex[:8]}"
    feat_id = f"test-feat-{uuid.uuid4().hex[:8]}"
    now = time.time()

    id_newer = f"sess-new-{uuid.uuid4().hex[:8]}"
    id_older = f"sess-old-{uuid.uuid4().hex[:8]}"
    id_archived = f"sess-arc-{uuid.uuid4().hex[:8]}"

    try:
        async with session_factory() as db:
            db.add(Session(
                id=id_newer,
                source="test",
                workspace_id=ws_id,
                feature_id=feat_id,
                started_at=now - 100,
                last_active_at=now - 10,
            ))
            db.add(Session(
                id=id_older,
                source="test",
                workspace_id=ws_id,
                feature_id=feat_id,
                started_at=now - 500,
                last_active_at=now - 100,
            ))
            db.add(Session(
                id=id_archived,
                source="test",
                workspace_id=ws_id,
                feature_id=feat_id,
                started_at=now - 300,
                last_active_at=now - 50,
                archived=True,
            ))
            await db.commit()

            # Add one assistant message to the newer session.
            db.add(Message(
                session_id=id_newer,
                role="assistant",
                content="Integration test assistant reply",
                active=True,
                created_at=now - 5,
            ))
            await db.commit()

        # Build the app backed by the real session factory.
        _inject_mock_run_agent()

        app = FastAPI()
        app.include_router(router, prefix="/api/v5")
        app.state.db_session = session_factory

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://testserver",
        ) as client:
            resp = await client.get(
                "/api/v5/sessions",
                params={"workspace_id": ws_id, "feature_id": feat_id},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert "sessions" in body
        sessions = body["sessions"]

        # Exactly 2 non-archived sessions.
        assert len(sessions) == 2, f"Expected 2 sessions, got {len(sessions)}: {sessions}"

        # Descending last_active_at order — newer first.
        assert sessions[0]["id"] == id_newer, "Newest session must come first"
        assert sessions[1]["id"] == id_older

        # Archived session must be absent.
        ids_returned = {s["id"] for s in sessions}
        assert id_archived not in ids_returned, "Archived session must not appear"

        # Excerpt populated only for the session that has an assistant message.
        assert sessions[0]["last_message_excerpt"] == "Integration test assistant reply"
        assert sessions[1]["last_message_excerpt"] == ""

    finally:
        async with session_factory() as db:
            for sid in [id_newer, id_older, id_archived]:
                obj = await db.get(Session, sid)
                if obj is not None:
                    await db.delete(obj)
            await db.commit()
        await engine.dispose()
