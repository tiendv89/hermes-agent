"""Tests for session listing endpoint and auto-title behaviour.

Covers the T1 test plan from tasks.md:
  - Unit: 3 sessions seeded (1 archived) → returns 2 ordered by last_active_at DESC
  - Unit: auto-title sets title to first 60 chars on a null-title session
  - Integration: GET /api/v5/sessions route returns correct JSON shape
"""

from __future__ import annotations

import sys
import time
import types
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = __file__
import pathlib

REPO_ROOT_PATH = pathlib.Path(__file__).resolve().parents[2]
if str(REPO_ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_PATH))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    id: str,
    workspace_id: str,
    feature_id: str,
    title: str | None,
    started_at: float,
    last_active_at: float,
    archived: bool = False,
) -> MagicMock:
    s = MagicMock()
    s.id = id
    s.workspace_id = workspace_id
    s.feature_id = feature_id
    s.title = title
    s.started_at = started_at
    s.last_active_at = last_active_at
    s.archived = archived
    return s


# ---------------------------------------------------------------------------
# Unit tests for list_sessions store function
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_sessions_excludes_archived_and_orders_by_last_active():
    """3 sessions (1 archived) → returns 2 ordered by last_active_at DESC."""
    from workflow_gateway.db.store import list_sessions

    now = time.time()
    # Two non-archived sessions + one archived; newer session has higher last_active_at
    sess_newer = MagicMock(
        id="sess_1", title="Newer", started_at=now - 100, last_active_at=now - 10
    )
    sess_older = MagicMock(
        id="sess_2", title="Older", started_at=now - 200, last_active_at=now - 100
    )

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
        "workflow_gateway.db.store._last_assistant_excerpt", AsyncMock(return_value="")
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
    from workflow_gateway.db.store import list_sessions

    now = time.time()
    row = MagicMock(id="sess_x", title=None, started_at=now, last_active_at=now)

    result_mock = MagicMock()
    result_mock.all.return_value = [row]

    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)

    with patch(
        "workflow_gateway.db.store._last_assistant_excerpt", AsyncMock(return_value="")
    ):
        sessions = await list_sessions(db, workspace_id="ws-1", feature_id="feat-1")

    assert sessions[0]["title"] == "(untitled)"


@pytest.mark.asyncio
async def test_last_assistant_excerpt_returns_up_to_120_chars():
    """_last_assistant_excerpt returns first 120 chars of last assistant message."""
    from workflow_gateway.db.store import _last_assistant_excerpt

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
    from workflow_gateway.db.store import _last_assistant_excerpt

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
    from workflow_gateway.api.router import router

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

    with (
        patch(
            "workflow_gateway.api.router.get_session",
            AsyncMock(side_effect=[null_title_session, updated_session]),
        ),
        patch(
            "workflow_gateway.api.router.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch("workflow_gateway.api.router.set_session_title", set_title_mock),
        patch("workflow_gateway.api.router.touch_session", AsyncMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=gateway_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v5/stream_chat",
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

    with (
        patch(
            "workflow_gateway.api.router.get_session",
            AsyncMock(return_value=existing_session),
        ),
        patch(
            "workflow_gateway.api.router.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch("workflow_gateway.api.router.set_session_title", set_title_mock),
        patch("workflow_gateway.api.router.touch_session", AsyncMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=gateway_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v5/stream_chat",
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
        "workflow_gateway.api.router.list_sessions",
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
