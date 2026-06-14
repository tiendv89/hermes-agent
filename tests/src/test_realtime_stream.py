"""Tests for T3: real-time SSE fan-out — bus, stream endpoint, typing, agent republish.

Covers the T3 test plan:
  - Cross-subscriber delivery: publish reaches all concurrent subscribers.
  - Agent-output fan-out: BusPublishingSSETranslator publishes structured events.
  - Replay on reconnect: ?since= query returns missed persisted messages.
  - Non-member rejection from GET .../stream (403).
  - Typing not persisted: POST .../typing publishes to bus, not DB.
  - Bus subscribe() context manager cleans up on exit.
  - Slow subscriber drops (QueueFull handled gracefully).
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Stub heavy deps
# ---------------------------------------------------------------------------


def _inject_stubs():
    for mod_name in ("run_agent", "hermes_state", "plugins", "plugins.context", "plugins.skills"):
        if mod_name not in sys.modules:
            sys.modules[mod_name] = types.ModuleType(mod_name)

    if not hasattr(sys.modules["run_agent"], "AIAgent"):
        sys.modules["run_agent"].AIAgent = MagicMock()

    if not hasattr(sys.modules["hermes_state"], "SessionDB"):
        class _FakeSessionDB:
            def append_message(self, *a, **kw):
                return 0
            def update_token_counts(self, *a, **kw):
                pass
        sys.modules["hermes_state"].SessionDB = _FakeSessionDB

    ctx = sys.modules.get("plugins.context") or types.ModuleType("plugins.context")
    if not hasattr(ctx, "set_context"):
        ctx.set_context = MagicMock()
        ctx.clear_context = MagicMock()
    sys.modules["plugins.context"] = ctx

    skills = sys.modules.get("plugins.skills") or types.ModuleType("plugins.skills")
    if not hasattr(skills, "get_shared_rules"):
        skills.get_shared_rules = lambda: None
    sys.modules["plugins.skills"] = skills


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stream_app(identity_user_id="user_a"):
    """Minimal FastAPI app with the stream router, using dependency overrides."""
    _inject_stubs()
    from fastapi import FastAPI
    from src.api.routers.stream import router as stream_router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    async def _override_db():
        yield mock_db

    app = FastAPI()
    app.include_router(stream_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = lambda: Identity(user_id=identity_user_id, org_id="org_1")
    return app


def _make_messages_app(identity_user_id="user_a"):
    """Minimal FastAPI app with the messages router, using dependency overrides."""
    _inject_stubs()
    from fastapi import FastAPI
    from src.api.routers.messages import router as messages_router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity
    from contextlib import asynccontextmanager

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    @asynccontextmanager
    async def _db_factory():
        yield mock_db

    async def _override_db():
        yield mock_db

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = lambda: Identity(user_id=identity_user_id, org_id="org_1")
    app.state.db_session = _db_factory
    return app


# ---------------------------------------------------------------------------
# In-process bus tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_publish_reaches_single_subscriber():
    """Events published reach a subscribed queue."""
    from src.realtime.bus import SessionBus

    bus = SessionBus()
    received = []

    async with bus.subscribe("sess_a") as q:
        bus.publish("sess_a", {"event": "test", "data": {}})
        event = await asyncio.wait_for(q.get(), timeout=1.0)
        received.append(event)

    assert received == [{"event": "test", "data": {}}]


@pytest.mark.asyncio
async def test_bus_publish_reaches_multiple_subscribers():
    """Fan-out: two subscribers both receive the same event."""
    from src.realtime.bus import SessionBus

    bus = SessionBus()
    results: list = []

    async with bus.subscribe("sess_b") as q1, bus.subscribe("sess_b") as q2:
        bus.publish("sess_b", {"event": "msg", "data": {"id": "1"}})

        e1 = await asyncio.wait_for(q1.get(), timeout=1.0)
        e2 = await asyncio.wait_for(q2.get(), timeout=1.0)
        results.extend([e1, e2])

    assert len(results) == 2
    assert all(r["event"] == "msg" for r in results)


@pytest.mark.asyncio
async def test_bus_different_topics_isolated():
    """Events on topic A are not received by topic B subscribers."""
    from src.realtime.bus import SessionBus

    bus = SessionBus()

    async with bus.subscribe("topic_a") as qa, bus.subscribe("topic_b") as qb:
        bus.publish("topic_a", {"event": "a_event", "data": {}})

        # topic_a subscriber gets it
        evt = await asyncio.wait_for(qa.get(), timeout=1.0)
        assert evt["event"] == "a_event"

        # topic_b subscriber should NOT get it
        assert qb.empty()


@pytest.mark.asyncio
async def test_bus_cleanup_on_exit():
    """After the context manager exits, the subscriber is removed."""
    from src.realtime.bus import SessionBus

    bus = SessionBus()

    async with bus.subscribe("sess_c") as q:
        pass  # exits immediately

    # No subscribers should remain.
    assert "sess_c" not in bus._topics


@pytest.mark.asyncio
async def test_bus_slow_subscriber_drops_gracefully():
    """A full subscriber queue causes QueueFull to be swallowed — other subs unaffected."""
    from src.realtime.bus import SessionBus, _MAX_QUEUE

    bus = SessionBus()

    async with bus.subscribe("sess_slow") as q_slow, bus.subscribe("sess_slow") as q_fast:
        # Fill the slow subscriber's queue to capacity.
        for i in range(_MAX_QUEUE):
            bus.publish("sess_slow", {"event": "fill", "data": {"i": i}})

        # The _MAX_QUEUE + 1th publish would overflow the slow queue — must not raise.
        bus.publish("sess_slow", {"event": "overflow", "data": {}})  # no exception

        # The fast subscriber received all events that fit (up to _MAX_QUEUE).
        assert not q_fast.empty()


@pytest.mark.asyncio
async def test_bus_no_subscribers_publish_is_noop():
    """publish() with no subscribers does not raise."""
    from src.realtime.bus import SessionBus

    bus = SessionBus()
    # No subscribers registered — should be a no-op.
    bus.publish("no_subs", {"event": "x", "data": {}})


# ---------------------------------------------------------------------------
# BusPublishingSSETranslator tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_translator_delta_published():
    """on_delta publishes agent.delta to the bus for the session."""
    from src.realtime.bus import SessionBus
    from src.streaming.bus_translator import BusPublishingSSETranslator

    bus = SessionBus()
    with patch("src.streaming.bus_translator.get_bus", return_value=bus):
        translator = BusPublishingSSETranslator(session_id="sess_t", model="test")

        async with bus.subscribe("sess_t") as q:
            translator.on_delta(delta="Hello")
            event = await asyncio.wait_for(q.get(), timeout=1.0)

    assert event["event"] == "agent.delta"
    assert event["data"]["content"] == "Hello"


@pytest.mark.asyncio
async def test_bus_translator_tool_progress_published():
    """on_tool_start / on_tool_complete publish hermes.tool.progress events."""
    from src.realtime.bus import SessionBus
    from src.streaming.bus_translator import BusPublishingSSETranslator

    bus = SessionBus()
    events = []

    with patch("src.streaming.bus_translator.get_bus", return_value=bus):
        translator = BusPublishingSSETranslator(session_id="sess_t2", model="test")

        async with bus.subscribe("sess_t2") as q:
            translator.on_tool_start(call_id="c1", name="my_tool")
            translator.on_tool_complete(call_id="c1", name="my_tool", output=None)

            while not q.empty():
                events.append(q.get_nowait())

    event_names = [e["event"] for e in events]
    assert "hermes.tool.progress" in event_names
    statuses = [e["data"]["status"] for e in events if e["event"] == "hermes.tool.progress"]
    assert "running" in statuses
    assert "completed" in statuses


@pytest.mark.asyncio
async def test_bus_translator_done_publishes_agent_done():
    """done() publishes agent.done with finish_reason=stop."""
    from src.realtime.bus import SessionBus
    from src.streaming.bus_translator import BusPublishingSSETranslator

    bus = SessionBus()
    events = []

    with patch("src.streaming.bus_translator.get_bus", return_value=bus):
        translator = BusPublishingSSETranslator(session_id="sess_t3", model="test")

        async with bus.subscribe("sess_t3") as q:
            translator.done()
            while not q.empty():
                events.append(q.get_nowait())

    done_events = [e for e in events if e["event"] == "agent.done"]
    assert done_events
    assert done_events[0]["data"]["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_bus_translator_error_publishes_agent_done():
    """on_error() publishes agent.done with finish_reason=error."""
    from src.realtime.bus import SessionBus
    from src.streaming.bus_translator import BusPublishingSSETranslator

    bus = SessionBus()
    events = []

    with patch("src.streaming.bus_translator.get_bus", return_value=bus):
        translator = BusPublishingSSETranslator(session_id="sess_t4", model="test")

        async with bus.subscribe("sess_t4") as q:
            translator.on_error(message="something went wrong")
            while not q.empty():
                events.append(q.get_nowait())

    done_events = [e for e in events if e["event"] == "agent.done"]
    assert done_events
    assert done_events[0]["data"]["finish_reason"] == "error"
    assert "error" in done_events[0]["data"]


@pytest.mark.asyncio
async def test_bus_translator_null_delta_not_published():
    """None/falsy delta does not publish an agent.delta event."""
    from src.realtime.bus import SessionBus
    from src.streaming.bus_translator import BusPublishingSSETranslator

    bus = SessionBus()

    with patch("src.streaming.bus_translator.get_bus", return_value=bus):
        translator = BusPublishingSSETranslator(session_id="sess_t5", model="test")

        events_before: list = []
        async with bus.subscribe("sess_t5") as q:
            translator.on_delta(delta=None)
            translator.on_delta(delta="")
            while not q.empty():
                events_before.append(q.get_nowait())

    delta_events = [e for e in events_before if e["event"] == "agent.delta"]
    assert not delta_events, "None/empty delta should not publish agent.delta"


# ---------------------------------------------------------------------------
# GET /threads/{id}/stream endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_non_member_returns_403():
    """Non-member calling GET .../stream is rejected with 403."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.stream import router as stream_router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity

    _inject_stubs()
    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()

    async def _override_db():
        yield mock_db

    app = FastAPI()
    app.include_router(stream_router, prefix="/api/v1")
    # Identity is "outsider", session owner is "owner"
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = lambda: Identity(user_id="outsider", org_id="org_1")

    session = MagicMock()
    session.user_id = "owner"

    with (
        patch("src.api.routers.stream.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.stream.is_member", AsyncMock(return_value=False)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/threads/sess_1/stream")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_stream_thread_not_found_returns_404():
    """Thread not found → 404."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.stream import router as stream_router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity

    _inject_stubs()
    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()

    async def _override_db():
        yield mock_db

    app = FastAPI()
    app.include_router(stream_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = lambda: Identity(user_id="user_a", org_id="org_1")

    with patch("src.api.routers.stream.get_session", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.get("/api/v1/threads/no_such/stream")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_stream_owner_can_connect_and_receives_events():
    """Session owner can open the SSE stream and receives events published to the bus.

    The stream terminates naturally when a channel.deleted event for the same
    session_id is published — this avoids an infinite-stream hang in tests.
    """
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.stream import router as stream_router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity
    from src.realtime.bus import get_bus

    _inject_stubs()
    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()

    async def _override_db():
        yield mock_db

    app = FastAPI()
    app.include_router(stream_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = lambda: Identity(user_id="owner_a", org_id="org_1")

    session = MagicMock()
    session.user_id = "owner_a"

    bus = get_bus()

    # Publish a ping then a channel.deleted to terminate the stream naturally.
    async def _send_and_terminate():
        await asyncio.sleep(0.05)
        bus.publish("sess_stream_test", {"event": "test.ping", "data": {"ok": True}})
        await asyncio.sleep(0.01)
        bus.publish(
            "sess_stream_test",
            {"event": "channel.deleted", "data": {"session_id": "sess_stream_test"}},
        )

    with (
        patch("src.api.routers.stream.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.stream.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.stream.get_messages_since", AsyncMock(return_value=[])),
    ):
        asyncio.create_task(_send_and_terminate())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", timeout=3.0
        ) as client:
            chunks = []
            async with client.stream(
                "GET", "/api/v1/threads/sess_stream_test/stream"
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers.get("content-type", "")
                async for line in resp.aiter_lines():
                    chunks.append(line)

    # stream terminated: should have received the ping and channel.deleted frames
    combined = "\n".join(chunks)
    assert "test.ping" in combined


@pytest.mark.asyncio
async def test_stream_replays_since_messages():
    """?since=<id> causes replay of missed messages before live stream."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.stream import router as stream_router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity

    _inject_stubs()
    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()

    async def _override_db():
        yield mock_db

    app = FastAPI()
    app.include_router(stream_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = lambda: Identity(user_id="user_a", org_id="org_1")

    session = MagicMock()
    session.user_id = "user_a"

    missed = [
        {
            "id": "101",
            "session_id": "sess_x2",
            "role": "user",
            "content": "old msg",
            "author_id": "user_a",
            "created_at": 1.0,
        }
    ]

    from src.realtime.bus import get_bus
    bus = get_bus()

    # Terminate the stream after replay by publishing channel.deleted.
    async def _terminate():
        await asyncio.sleep(0.1)
        bus.publish(
            "sess_x2",
            {"event": "channel.deleted", "data": {"session_id": "sess_x2"}},
        )

    with (
        patch("src.api.routers.stream.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.stream.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.stream.get_messages_since", AsyncMock(return_value=missed)) as mock_since,
    ):
        asyncio.create_task(_terminate())
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test", timeout=3.0
        ) as client:
            chunks = []
            async with client.stream(
                "GET", "/api/v1/threads/sess_x2/stream?since=100"
            ) as resp:
                assert resp.status_code == 200
                async for line in resp.aiter_lines():
                    chunks.append(line)

    mock_since.assert_called_once()
    call_args = mock_since.call_args
    # Second positional arg is session_id, third is since_id
    assert call_args[0][2] == 100

    combined = "\n".join(chunks)
    assert "message.created" in combined
    assert "old msg" in combined


# ---------------------------------------------------------------------------
# POST /threads/{id}/typing endpoint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_typing_published_not_persisted():
    """POST .../typing publishes a typing event to the bus without DB write."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.stream import router as stream_router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity
    from src.realtime.bus import SessionBus

    _inject_stubs()
    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    async def _override_db():
        yield mock_db

    app = FastAPI()
    app.include_router(stream_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = lambda: Identity(user_id="user_a", org_id="org_1")

    session = MagicMock()
    session.user_id = "user_a"

    fake_bus = SessionBus()
    published: list = []

    original_publish = fake_bus.publish
    def _capture(session_id, event):
        published.append((session_id, event))
        original_publish(session_id, event)
    fake_bus.publish = _capture

    with (
        patch("src.api.routers.stream.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.stream.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.stream.get_bus", return_value=fake_bus),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/threads/sess_typing/typing")

    assert resp.status_code == 204
    assert any(e[1]["event"] == "typing" for e in published)
    typing_event = next(e[1] for e in published if e[1]["event"] == "typing")
    assert typing_event["data"]["user_id"] == "user_a"
    assert typing_event["data"]["session_id"] == "sess_typing"
    # DB commit should NOT have been called (ephemeral, not persisted)
    mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_typing_non_member_returns_403():
    """Non-member typing → 403."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.stream import router as stream_router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity

    _inject_stubs()
    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()

    async def _override_db():
        yield mock_db

    app = FastAPI()
    app.include_router(stream_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = lambda: Identity(user_id="outsider", org_id="org_1")

    session = MagicMock()
    session.user_id = "owner"

    with (
        patch("src.api.routers.stream.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.stream.is_member", AsyncMock(return_value=False)),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/threads/sess_typing/typing")

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_typing_thread_not_found_returns_404():
    """Thread not found → 404 on typing endpoint."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.stream import router as stream_router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity

    _inject_stubs()
    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()

    async def _override_db():
        yield mock_db

    app = FastAPI()
    app.include_router(stream_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = lambda: Identity(user_id="user_a", org_id="org_1")

    with patch("src.api.routers.stream.get_session", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post("/api/v1/threads/nope/typing")

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Integration: send service publishes message.created to bus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_publishes_to_bus():
    """POST /threads/{id}/messages publishes a message.created bus event."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity
    from src.realtime.bus import SessionBus
    from contextlib import asynccontextmanager

    _inject_stubs()
    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    @asynccontextmanager
    async def _db_factory():
        yield mock_db

    async def _override_db():
        yield mock_db

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = lambda: Identity(user_id="user_a", org_id="org_1")
    app.state.db_session = _db_factory

    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "channel"
    session.feature_id = ""
    session.workspace_id = "ws-1"
    session.model = None

    fake_bus = SessionBus()
    published: list = []
    original_publish = fake_bus.publish

    def _capture(sid, event):
        published.append((sid, event))
        original_publish(sid, event)

    fake_bus.publish = _capture

    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.list_members", AsyncMock(return_value=[])),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=55)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch("src.api.routers.messages.get_bus", return_value=fake_bus),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            resp = await client.post(
                "/api/v1/threads/sess_pub/messages",
                json={"content": "hello channel"},
            )

    assert resp.status_code == 202

    msg_events = [e[1] for e in published if e[1]["event"] == "message.created"]
    assert msg_events, "Expected message.created bus event"
    data = msg_events[0]["data"]
    assert data["id"] == "55"
    assert data["content"] == "hello channel"
    assert data["author_id"] == "user_a"


# ---------------------------------------------------------------------------
# Agent dispatch publishes agent.working
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_agent_turn_publishes_agent_working():
    """schedule_agent_turn publishes agent.working to the bus before the executor starts."""
    _inject_stubs()
    from src.api.agent_dispatch import (
        _active_runs,
        _active_runs_lock,
        schedule_agent_turn,
    )
    from src.realtime.bus import SessionBus

    session_id = "sess_working_test2"
    with _active_runs_lock:
        _active_runs.discard(session_id)

    fake_bus = SessionBus()
    published: list = []
    original_publish = fake_bus.publish

    def _capture(sid, event):
        published.append((sid, event))
        original_publish(sid, event)

    fake_bus.publish = _capture

    loop = asyncio.get_event_loop()
    db_factory_mock = AsyncMock()

    with (
        patch("src.api.agent_dispatch.BusPublishingSSETranslator", MagicMock()),
        patch("src.api.agent_dispatch.get_bus", return_value=fake_bus),
        patch.object(loop, "run_in_executor", MagicMock()),
    ):
        await schedule_agent_turn(
            session_id=session_id,
            message="hello",
            history=[],
            workspace_id="ws-1",
            feature_id="feat-1",
            user_id="user_a",
            model="test-model",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=db_factory_mock,
            loop=loop,
        )

    working_events = [e[1] for e in published if e[1]["event"] == "agent.working"]
    assert working_events, "Expected agent.working published to bus"
    assert working_events[0]["data"]["session_id"] == session_id

    # Cleanup.
    with _active_runs_lock:
        _active_runs.discard(session_id)
