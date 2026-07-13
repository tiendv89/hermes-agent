"""Tests for T2: @mention parse/resolve + @agent-gated dispatch + send service.

Covers the T2 test plan:
  - Explicit @agent ⇒ one agent turn triggered
  - Channel bare message ⇒ no run_conversation
  - Feature thread bare message ⇒ one agent turn triggered
  - Rapid @agent mentions ⇒ one coalesced turn (not two)
  - Channel context: feature tools not triggered (feature_id='')
  - Unknown handles handled gracefully
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Stubs for heavyweight deps not installed in the test env
# ---------------------------------------------------------------------------


def _inject_stub_modules():
    if "run_agent" not in sys.modules:
        stub = types.ModuleType("run_agent")
        stub.AIAgent = MagicMock()  # type: ignore[attr-defined]
        sys.modules["run_agent"] = stub

    if "hermes_state" not in sys.modules:
        stub = types.ModuleType("hermes_state")

        class _FakeSessionDB:
            def append_message(self, *a, **kw):
                return 0

            def update_token_counts(self, *a, **kw):
                pass

        stub.SessionDB = _FakeSessionDB  # type: ignore[attr-defined]
        sys.modules["hermes_state"] = stub

    for _mod in ("plugins", "plugins.context", "plugins.skills"):
        if _mod not in sys.modules:
            m = types.ModuleType(_mod)
            sys.modules[_mod] = m

    plugins = sys.modules["plugins"]
    if not hasattr(plugins, "context"):
        ctx = types.ModuleType("plugins.context")
        ctx.set_context = MagicMock()  # type: ignore[attr-defined]
        ctx.clear_context = MagicMock()  # type: ignore[attr-defined]
        sys.modules["plugins.context"] = ctx
        plugins.context = ctx  # type: ignore[attr-defined]

    skills_mod = sys.modules.get("plugins.skills")
    if skills_mod is None or not hasattr(skills_mod, "get_shared_rules"):
        skills_mod = types.ModuleType("plugins.skills")
        skills_mod.get_shared_rules = lambda: None  # type: ignore[attr-defined]
        sys.modules["plugins.skills"] = skills_mod


# ---------------------------------------------------------------------------
# mention parsing tests
# ---------------------------------------------------------------------------


def test_parse_handles_basic():
    from src.api.mentions import parse_mention_handles

    assert parse_mention_handles("Hello @agent, cc @alice!") == ["agent", "alice"]


def test_parse_handles_deduped():
    from src.api.mentions import parse_mention_handles

    assert parse_mention_handles("@agent @agent @bob @agent") == ["agent", "bob"]


def test_parse_handles_empty():
    from src.api.mentions import parse_mention_handles

    assert parse_mention_handles("No mentions here") == []


def test_parse_handles_case_insensitive():
    from src.api.mentions import parse_mention_handles

    assert parse_mention_handles("@Agent says hi") == ["agent"]


def test_mentions_agent_true():
    from src.api.mentions import mentions_agent

    assert mentions_agent("Hey @agent, help!") is True


def test_mentions_agent_false():
    from src.api.mentions import mentions_agent

    assert mentions_agent("Hello everyone") is False


# ---------------------------------------------------------------------------
# mention resolution tests
# ---------------------------------------------------------------------------


def test_resolve_agent_sentinel():
    from src.api.mentions import resolve_mentions

    result = resolve_mentions(["agent"], [])
    assert result == [{"mentioned_id": "agent", "mentioned_kind": "agent"}]


def test_resolve_user_by_handle():
    from src.api.mentions import resolve_mentions

    members = [{"user_id": "usr_1", "handle": "alice"}]
    result = resolve_mentions(["alice"], members)
    assert result == [{"mentioned_id": "usr_1", "mentioned_kind": "user"}]


def test_resolve_user_by_username_fallback():
    from src.api.mentions import resolve_mentions

    members = [{"user_id": "usr_2", "username": "bob"}]
    result = resolve_mentions(["bob"], members)
    assert result == [{"mentioned_id": "usr_2", "mentioned_kind": "user"}]


def test_resolve_unknown_handle_skipped():
    from src.api.mentions import resolve_mentions

    members = [{"user_id": "usr_1", "handle": "alice"}]
    result = resolve_mentions(["ghost"], members)
    assert result == []


def test_resolve_mixed():
    from src.api.mentions import resolve_mentions

    members = [{"user_id": "usr_1", "handle": "alice"}]
    result = resolve_mentions(["agent", "alice", "ghost"], members)
    assert len(result) == 2
    kinds = {r["mentioned_kind"] for r in result}
    assert kinds == {"agent", "user"}


def test_resolve_deduped_ids():
    from src.api.mentions import resolve_mentions

    members = [{"user_id": "usr_1", "handle": "alice"}]
    result = resolve_mentions(["alice", "alice"], members)
    assert len(result) == 1


# ---------------------------------------------------------------------------
# dispatch gate tests
# ---------------------------------------------------------------------------


def _make_session(kind="thread", feature_id="feat-1"):
    s = MagicMock()
    s.kind = kind
    s.feature_id = feature_id
    return s


def test_dispatch_gate_explicit_agent():
    from src.api.routers.messages import _should_trigger_agent

    session = _make_session(kind="thread", feature_id="feat-1")
    assert _should_trigger_agent(session, has_explicit_agent_mention=True) is True


def test_dispatch_gate_channel_explicit_agent():
    from src.api.routers.messages import _should_trigger_agent

    session = _make_session(kind="channel", feature_id="")
    assert _should_trigger_agent(session, has_explicit_agent_mention=True) is True


def test_dispatch_gate_channel_bare_no_trigger():
    """Channel + bare message ⇒ no agent turn."""
    from src.api.routers.messages import _should_trigger_agent

    session = _make_session(kind="channel", feature_id="")
    assert _should_trigger_agent(session, has_explicit_agent_mention=False) is False


def test_dispatch_gate_feature_thread_bare_triggers():
    """Feature thread + bare message ⇒ agent turn (v3 feel)."""
    from src.api.routers.messages import _should_trigger_agent

    session = _make_session(kind="thread", feature_id="some-feature")
    assert _should_trigger_agent(session, has_explicit_agent_mention=False) is True


def test_dispatch_gate_thread_no_feature_bare_triggers():
    """Ad-hoc workspace thread (no feature_id) + bare message ⇒ agent turn (no @agent required)."""
    from src.api.routers.messages import _should_trigger_agent

    session = _make_session(kind="thread", feature_id="")
    assert _should_trigger_agent(session, has_explicit_agent_mention=False) is True


# ---------------------------------------------------------------------------
# send-service HTTP endpoint tests
# ---------------------------------------------------------------------------


def _make_db_session_factory(mock_db):
    @asynccontextmanager
    async def _factory():
        yield mock_db

    return _factory


def _make_app(session_mock=None, member_check=True):
    """Minimal FastAPI app wired to the messages router."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=None)
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.execute = AsyncMock()

    app.state.db_session = _make_db_session_factory(mock_db)
    return app, mock_db


@pytest.mark.asyncio
async def test_send_message_explicit_agent_triggers(tmp_path):
    """POST /threads/{id}/messages with @agent → 202, agent_triggered=True."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    # Session mock: feature thread
    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "thread"
    session.feature_id = "feat-1"
    session.workspace_id = "ws-1"
    session.model = None

    # Members mock
    members_result = MagicMock()
    members_result.scalars.return_value.all.return_value = []

    # History mock
    history_result = MagicMock()
    history_result.scalars.return_value.all.return_value = []

    _model = {"model": "test-model", "provider": None, "api_key": None, "base_url": None}
    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=42)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch("src.api.routers.messages.default_model", AsyncMock(return_value="test-model")),
        patch("src.api.routers.messages.resolve_model", AsyncMock(return_value=_model)),
        patch("src.api.routers.messages.update_session_model", AsyncMock()),
        patch(
            "src.api.routers.messages.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.api.routers.messages.schedule_agent_turn", AsyncMock(return_value=True)
        ) as mock_dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_1/messages",
                json={"content": "hey @agent, help me"},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["agent_triggered"] is True
    assert body["message_id"] == "42"
    mock_dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_forwards_image_ids_to_schedule_agent_turn():
    """POST /threads/{id}/messages with image_ids forwards them to
    schedule_agent_turn — this is the real production send path (via
    sendThreadMessage), unlike the legacy /chat endpoint's image_ids."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "thread"
    session.feature_id = "feat-1"
    session.workspace_id = "ws-1"
    session.model = None

    _model = {"model": "test-model", "provider": None, "api_key": None, "base_url": None}
    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=42)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch("src.api.routers.messages.default_model", AsyncMock(return_value="test-model")),
        patch("src.api.routers.messages.resolve_model", AsyncMock(return_value=_model)),
        patch("src.api.routers.messages.update_session_model", AsyncMock()),
        patch(
            "src.api.routers.messages.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.api.routers.messages.schedule_agent_turn", AsyncMock(return_value=True)
        ) as mock_dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_1/messages",
                json={"content": "what's in this image?", "image_ids": ["img-1", "img-2"]},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 202
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.kwargs["image_ids"] == ["img-1", "img-2"]


@pytest.mark.asyncio
async def test_send_message_without_image_ids_defaults_to_empty_list():
    """POST /threads/{id}/messages without image_ids forwards an empty list,
    not None — schedule_agent_turn's coalescing dict stores it verbatim."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "thread"
    session.feature_id = "feat-1"
    session.workspace_id = "ws-1"
    session.model = None

    _model = {"model": "test-model", "provider": None, "api_key": None, "base_url": None}
    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=42)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch("src.api.routers.messages.default_model", AsyncMock(return_value="test-model")),
        patch("src.api.routers.messages.resolve_model", AsyncMock(return_value=_model)),
        patch("src.api.routers.messages.update_session_model", AsyncMock()),
        patch(
            "src.api.routers.messages.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.api.routers.messages.schedule_agent_turn", AsyncMock(return_value=True)
        ) as mock_dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_1/messages",
                json={"content": "hey @agent"},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 202
    mock_dispatch.assert_called_once()
    assert mock_dispatch.call_args.kwargs["image_ids"] == []


@pytest.mark.asyncio
async def test_send_message_channel_bare_no_agent():
    """POST /threads/{channel_id}/messages bare message → 202, agent_triggered=False."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    # Channel session: kind='channel', no feature_id
    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "channel"
    session.feature_id = ""
    session.workspace_id = "ws-1"
    session.model = None

    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=10)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch(
            "src.api.routers.messages.schedule_agent_turn", AsyncMock()
        ) as mock_dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_ch/messages",
                json={"content": "Hello channel, no agent here"},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["agent_triggered"] is False
    mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_feature_thread_bare_triggers():
    """POST /threads/{id}/messages bare message in feature thread → agent triggered."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    # Feature thread: kind='thread', feature_id set
    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "thread"
    session.feature_id = "feat-xyz"
    session.workspace_id = "ws-1"
    session.model = None

    _model = {"model": "test-model", "provider": None, "api_key": None, "base_url": None}
    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=7)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch("src.api.routers.messages.default_model", AsyncMock(return_value="test-model")),
        patch("src.api.routers.messages.resolve_model", AsyncMock(return_value=_model)),
        patch("src.api.routers.messages.update_session_model", AsyncMock()),
        patch(
            "src.api.routers.messages.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.api.routers.messages.schedule_agent_turn", AsyncMock(return_value=True)
        ) as mock_dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_2/messages",
                json={"content": "What is the status?"},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["agent_triggered"] is True
    mock_dispatch.assert_called_once()


@pytest.mark.asyncio
async def test_send_message_not_member_returns_403():
    """Caller who is neither owner nor member gets 403."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    session = MagicMock()
    session.user_id = "owner_x"
    session.kind = "thread"
    session.feature_id = "feat-1"
    session.workspace_id = "ws-1"

    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_1/messages",
                json={"content": "Hi!"},
                headers={"X-User-Id": "outsider"},
            )

    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_send_message_unknown_thread_returns_404():
    """Non-existent session → 404."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    app.state.db_session = _make_db_session_factory(mock_db)

    with patch("src.api.routers.messages.get_session", AsyncMock(return_value=None)):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/no_such_sess/messages",
                json={"content": "Hi"},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Coalescing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coalescing_second_agent_mention_does_not_start_new_turn():
    """@agent while turn in-flight → coalesced (pending recorded, no new immediate turn)."""
    _inject_stub_modules()
    from src.api.agent_dispatch import (
        _active_runs,
        _active_runs_lock,
        _pending_agent_turns,
        _pending_lock,
        schedule_agent_turn,
    )

    session_id = "sess_coalesce_test"
    from src.api.agent_dispatch import ActiveRun

    # Simulate an in-flight turn.
    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(run_id="test-run-coalesce", task=None, triggered_by="user_a")

    # Clear any stale pending state.
    with _pending_lock:
        _pending_agent_turns.pop(session_id, None)

    db_factory_mock = AsyncMock()
    loop = asyncio.get_event_loop()

    with patch("src.api.agent_dispatch.HermesSSETranslator"):
        result = await schedule_agent_turn(
            session_id=session_id,
            message="@agent help again",
            history=[],
            workspace_id="ws-1",
            feature_id="feat-1",
            user_id="user_a",
            model="claude-sonnet-4-6",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=db_factory_mock,
            loop=loop,
        )

    # Should NOT have started a new turn (return False = coalesced).
    assert result is False

    # A pending record should be stored.
    with _pending_lock:
        pending = _pending_agent_turns.get(session_id)

    assert pending is not None
    assert pending["message"] == "@agent help again"

    # Cleanup.
    with _active_runs_lock:
        _active_runs.pop(session_id, None)
    with _pending_lock:
        _pending_agent_turns.pop(session_id, None)


@pytest.mark.asyncio
async def test_schedule_agent_turn_when_no_active_run():
    """schedule_agent_turn returns True and starts a turn when session is free."""
    _inject_stub_modules()
    from src.api.agent_dispatch import (
        _active_runs,
        _active_runs_lock,
        schedule_agent_turn,
    )

    session_id = "sess_free_test"
    with _active_runs_lock:
        _active_runs.pop(session_id, None)

    db_factory_mock = AsyncMock()
    loop = asyncio.get_event_loop()

    async def _noop_turn(**kwargs):
        pass

    with (
        patch("src.api.agent_dispatch._run_agent_turn_async", new=_noop_turn),
        patch("src.api.agent_dispatch.BusPublishingSSETranslator", MagicMock()),
        patch(
            "src.api.agent_dispatch.get_bus",
            MagicMock(return_value=MagicMock(publish=MagicMock())),
        ),
    ):
        result = await schedule_agent_turn(
            session_id=session_id,
            message="hello @agent",
            history=[],
            workspace_id="ws-1",
            feature_id="feat-1",
            user_id="user_a",
            model="claude-sonnet-4-6",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=db_factory_mock,
            loop=loop,
        )

    assert result is True

    # Cleanup: remove from active_runs so tests are isolated.
    with _active_runs_lock:
        _active_runs.pop(session_id, None)


# ---------------------------------------------------------------------------
# Container-aware context tests
# ---------------------------------------------------------------------------


def test_channel_session_dispatch_gate_no_feature_tools():
    """Channel session (feature_id='') should not trigger for bare messages.

    The absence of feature_id is the guard against feature authoring tools in
    channels (NG12) — no new guard code needed.
    """
    from src.api.routers.messages import _should_trigger_agent

    # Channel: kind='channel', feature_id='' → bare message = no trigger
    session = MagicMock()
    session.kind = "channel"
    session.feature_id = ""
    assert _should_trigger_agent(session, False) is False
    # But explicit @agent still triggers even in channel
    assert _should_trigger_agent(session, True) is True


# ---------------------------------------------------------------------------
# DM dispatch gate tests (T2 — DM follows Channel bare-message rule)
# ---------------------------------------------------------------------------


def test_dispatch_gate_dm_bare_no_trigger():
    """DM + bare message → no agent turn (same as channel rule, §design §2)."""
    from src.api.routers.messages import _should_trigger_agent

    session = _make_session(kind="dm", feature_id="")
    assert _should_trigger_agent(session, has_explicit_agent_mention=False) is False


def test_dispatch_gate_dm_explicit_agent_triggers():
    """DM + explicit @agent → agent turn is triggered."""
    from src.api.routers.messages import _should_trigger_agent

    session = _make_session(kind="dm", feature_id="")
    assert _should_trigger_agent(session, has_explicit_agent_mention=True) is True


@pytest.mark.asyncio
async def test_send_message_dm_bare_no_agent():
    """POST /threads/{dm_id}/messages bare message in DM → 202, agent_triggered=False."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    # DM session: kind='dm', no feature_id (mirrors channel test)
    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "dm"
    session.feature_id = ""
    session.workspace_id = "ws-1"
    session.model = None

    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=20)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch(
            "src.api.routers.messages.schedule_agent_turn", AsyncMock()
        ) as mock_dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_dm/messages",
                json={"content": "Hey, what are you doing later?"},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["agent_triggered"] is False
    mock_dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_send_message_dm_explicit_agent_triggers():
    """POST /threads/{dm_id}/messages with @agent in DM → 202, agent_triggered=True."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    # DM session: kind='dm', no feature_id
    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "dm"
    session.feature_id = ""
    session.workspace_id = "ws-1"
    session.model = None

    _model = {"model": "test-model", "provider": None, "api_key": None, "base_url": None}
    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=21)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch("src.api.routers.messages.default_model", AsyncMock(return_value="test-model")),
        patch("src.api.routers.messages.resolve_model", AsyncMock(return_value=_model)),
        patch("src.api.routers.messages.update_session_model", AsyncMock()),
        patch(
            "src.api.routers.messages.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.api.routers.messages.schedule_agent_turn", AsyncMock(return_value=True)
        ) as mock_dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_dm/messages",
                json={"content": "@agent what is the status of feature VOY-59?"},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 202
    body = resp.json()
    assert body["agent_triggered"] is True
    assert body["message_id"] == "21"
    mock_dispatch.assert_called_once()


# ---------------------------------------------------------------------------
# _agent_reply_thread_context unit tests (T1: auto-thread for channel/DM)
# ---------------------------------------------------------------------------


def _make_msg(msg_id, thread_root_id=None):
    from types import SimpleNamespace
    return SimpleNamespace(id=msg_id, thread_root_id=thread_root_id)


def test_agent_reply_thread_context_g1_in_thread():
    """G1: mention inside existing thread → passthrough thread context unchanged."""
    from src.api.routers.messages import _agent_reply_thread_context

    session = _make_session(kind="channel", feature_id="")
    msg = _make_msg(msg_id=99, thread_root_id=10)
    root, reply_to = _agent_reply_thread_context(session, msg)
    assert root == 10
    assert reply_to == 99


def test_agent_reply_thread_context_g2_channel_top_level():
    """G2: channel top-level mention → auto-open a thread at the triggering message."""
    from src.api.routers.messages import _agent_reply_thread_context

    session = _make_session(kind="channel", feature_id="")
    msg = _make_msg(msg_id=42, thread_root_id=None)
    root, reply_to = _agent_reply_thread_context(session, msg)
    assert root == 42
    assert reply_to == 42


def test_agent_reply_thread_context_g2_dm_top_level():
    """G2: DM top-level mention → auto-open a thread at the triggering message."""
    from src.api.routers.messages import _agent_reply_thread_context

    session = _make_session(kind="dm", feature_id="")
    msg = _make_msg(msg_id=55, thread_root_id=None)
    root, reply_to = _agent_reply_thread_context(session, msg)
    assert root == 55
    assert reply_to == 55


def test_agent_reply_thread_context_g3_feature_thread_top_level():
    """G3: feature thread top-level mention → flat reply (thread_root_id=None)."""
    from src.api.routers.messages import _agent_reply_thread_context

    session = _make_session(kind="thread", feature_id="feat-xyz")
    msg = _make_msg(msg_id=77, thread_root_id=None)
    root, reply_to = _agent_reply_thread_context(session, msg)
    assert root is None
    assert reply_to is None


def test_agent_reply_thread_context_g1_feature_thread_in_thread():
    """G1 applies even in feature thread when mention is inside an existing thread."""
    from src.api.routers.messages import _agent_reply_thread_context

    session = _make_session(kind="thread", feature_id="feat-xyz")
    msg = _make_msg(msg_id=88, thread_root_id=20)
    root, reply_to = _agent_reply_thread_context(session, msg)
    assert root == 20
    assert reply_to == 88


def test_agent_reply_thread_context_g3_adhoc_thread_top_level():
    """G3: ad-hoc thread (kind='thread', feature_id='') top-level mention → flat reply."""
    from src.api.routers.messages import _agent_reply_thread_context

    session = _make_session(kind="thread", feature_id="")
    msg = _make_msg(msg_id=33, thread_root_id=None)
    root, reply_to = _agent_reply_thread_context(session, msg)
    assert root is None
    assert reply_to is None


# ---------------------------------------------------------------------------
# send_message thread_root_id propagation tests (HTTP integration)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_channel_top_level_agent_mention_opens_thread():
    """Channel top-level @agent mention → schedule_agent_turn called with
    thread_root_id=message_id and reply_to_message_id=message_id (G2)."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "channel"
    session.feature_id = ""
    session.workspace_id = "ws-1"
    session.model = None

    _model = {"model": "test-model", "provider": None, "api_key": None, "base_url": None}
    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=100)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch("src.api.routers.messages.default_model", AsyncMock(return_value="test-model")),
        patch("src.api.routers.messages.resolve_model", AsyncMock(return_value=_model)),
        patch("src.api.routers.messages.update_session_model", AsyncMock()),
        patch(
            "src.api.routers.messages.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.api.routers.messages.schedule_agent_turn", AsyncMock(return_value=True)
        ) as mock_dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_ch/messages",
                json={"content": "@agent help me with this"},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 202
    assert resp.json()["agent_triggered"] is True
    mock_dispatch.assert_called_once()
    kwargs = mock_dispatch.call_args.kwargs
    assert kwargs["thread_root_id"] == 100
    assert kwargs["reply_to_message_id"] == 100


@pytest.mark.asyncio
async def test_send_message_dm_top_level_agent_mention_opens_thread():
    """DM top-level @agent mention → schedule_agent_turn called with
    thread_root_id=message_id and reply_to_message_id=message_id (G2)."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "dm"
    session.feature_id = ""
    session.workspace_id = "ws-1"
    session.model = None

    _model = {"model": "test-model", "provider": None, "api_key": None, "base_url": None}
    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=200)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch("src.api.routers.messages.default_model", AsyncMock(return_value="test-model")),
        patch("src.api.routers.messages.resolve_model", AsyncMock(return_value=_model)),
        patch("src.api.routers.messages.update_session_model", AsyncMock()),
        patch(
            "src.api.routers.messages.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.api.routers.messages.schedule_agent_turn", AsyncMock(return_value=True)
        ) as mock_dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_dm/messages",
                json={"content": "@agent what is today's task?"},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 202
    assert resp.json()["agent_triggered"] is True
    mock_dispatch.assert_called_once()
    kwargs = mock_dispatch.call_args.kwargs
    assert kwargs["thread_root_id"] == 200
    assert kwargs["reply_to_message_id"] == 200


@pytest.mark.asyncio
async def test_send_message_feature_thread_top_level_agent_stays_flat():
    """Feature thread top-level @agent mention → schedule_agent_turn called with
    thread_root_id=None and reply_to_message_id=None (G3 — unchanged behavior)."""
    _inject_stub_modules()
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from src.api.routers.messages import router as messages_router

    app = FastAPI()
    app.include_router(messages_router, prefix="/api/v1")

    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()
    mock_db.add = MagicMock()

    app.state.db_session = _make_db_session_factory(mock_db)

    session = MagicMock()
    session.user_id = "user_a"
    session.kind = "thread"
    session.feature_id = "feat-abc"
    session.workspace_id = "ws-1"
    session.model = None

    _model = {"model": "test-model", "provider": None, "api_key": None, "base_url": None}
    with (
        patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.messages.is_member", AsyncMock(return_value=False)),
        patch("src.api.routers.messages.append_message", AsyncMock(return_value=300)),
        patch("src.api.routers.messages.persist_mentions", AsyncMock()),
        patch("src.api.routers.messages.touch_session", AsyncMock()),
        patch("src.api.routers.messages.default_model", AsyncMock(return_value="test-model")),
        patch("src.api.routers.messages.resolve_model", AsyncMock(return_value=_model)),
        patch("src.api.routers.messages.update_session_model", AsyncMock()),
        patch(
            "src.api.routers.messages.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch(
            "src.api.routers.messages.schedule_agent_turn", AsyncMock(return_value=True)
        ) as mock_dispatch,
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/threads/sess_ft/messages",
                json={"content": "@agent update this feature"},
                headers={"X-User-Id": "user_a"},
            )

    assert resp.status_code == 202
    assert resp.json()["agent_triggered"] is True
    mock_dispatch.assert_called_once()
    kwargs = mock_dispatch.call_args.kwargs
    assert kwargs["thread_root_id"] is None
    assert kwargs["reply_to_message_id"] is None
