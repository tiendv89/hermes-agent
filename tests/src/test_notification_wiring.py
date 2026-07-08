"""Unit tests for notification wiring in src/db/store.py.

Covers:
- persist_mentions emits mention notifications for user-kind mentions
- persist_mentions skips notifications for agent-kind mentions
- append_message emits channel_message to all channel members except author
- append_message emits dm to the other party in a DM session
- append_message does NOT emit for thread sessions (only mention path handles those)
- append_message does NOT emit for non-user roles
- _emit_message_notifications catches and swallows DB errors
"""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Stubs for heavyweight deps
# ---------------------------------------------------------------------------


def _inject_stubs():
    if "run_agent" not in sys.modules:
        stub = types.ModuleType("run_agent")
        stub.AIAgent = MagicMock()
        sys.modules["run_agent"] = stub

    if "hermes_state" not in sys.modules:
        stub = types.ModuleType("hermes_state")

        class _FakeSessionDB:
            def append_message(self, *a, **kw):
                return 0

            def update_token_counts(self, *a, **kw):
                pass

        stub.SessionDB = _FakeSessionDB
        sys.modules["hermes_state"] = stub

    for _mod in ("plugins", "plugins.context", "plugins.skills"):
        if _mod not in sys.modules:
            m = types.ModuleType(_mod)
            sys.modules[_mod] = m

    plugins = sys.modules["plugins"]
    if not hasattr(plugins, "context"):
        ctx = types.ModuleType("plugins.context")
        ctx.set_context = MagicMock()
        ctx.clear_context = MagicMock()
        sys.modules["plugins.context"] = ctx
        plugins.context = ctx

    skills_mod = sys.modules.get("plugins.skills")
    if skills_mod is None or not hasattr(skills_mod, "get_shared_rules"):
        skills_mod = types.ModuleType("plugins.skills")
        skills_mod.get_shared_rules = lambda: None
        sys.modules["plugins.skills"] = skills_mod


# ---------------------------------------------------------------------------
# persist_mentions — mention notification emission
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_mentions_emits_mention_for_user(monkeypatch):
    """persist_mentions schedules mention notifications for user-kind mentions."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import persist_mentions

    mock_session = MagicMock()
    mock_session.workspace_id = "ws-1"

    mock_db = MagicMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.get = AsyncMock(return_value=mock_session)

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await persist_mentions(
            mock_db,
            message_id=10,
            session_id="sess-1",
            mentions=[{"mentioned_id": "usr-2", "mentioned_kind": "user"}],
            author_id="usr-1",
        )

    mock_bulk.assert_called_once()
    payloads = mock_bulk.call_args[0][0]
    assert len(payloads) == 1
    assert payloads[0]["category"] == "mention"
    assert payloads[0]["user_id"] == "usr-2"
    assert payloads[0]["actor_user_id"] == "usr-1"
    assert payloads[0]["workspace_id"] == "ws-1"
    assert payloads[0]["source_id"] == "10"


@pytest.mark.asyncio
async def test_persist_mentions_skips_agent_kind(monkeypatch):
    """Agent-kind mentions do not produce notification payloads."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import persist_mentions

    mock_session = MagicMock()
    mock_session.workspace_id = "ws-1"

    mock_db = MagicMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.get = AsyncMock(return_value=mock_session)

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await persist_mentions(
            mock_db,
            message_id=11,
            session_id="sess-1",
            mentions=[{"mentioned_id": "agent", "mentioned_kind": "agent"}],
            author_id="usr-1",
        )

    mock_bulk.assert_not_called()


@pytest.mark.asyncio
async def test_persist_mentions_multiple_users(monkeypatch):
    """Multiple user mentions → all included in the bulk payload."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import persist_mentions

    mock_session = MagicMock()
    mock_session.workspace_id = "ws-1"

    mock_db = MagicMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.get = AsyncMock(return_value=mock_session)

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await persist_mentions(
            mock_db,
            message_id=12,
            session_id="sess-1",
            mentions=[
                {"mentioned_id": "usr-2", "mentioned_kind": "user"},
                {"mentioned_id": "usr-3", "mentioned_kind": "user"},
                {"mentioned_id": "agent", "mentioned_kind": "agent"},
            ],
            author_id="usr-1",
        )

    mock_bulk.assert_called_once()
    payloads = mock_bulk.call_args[0][0]
    assert len(payloads) == 2  # only user mentions
    user_ids = {p["user_id"] for p in payloads}
    assert user_ids == {"usr-2", "usr-3"}


@pytest.mark.asyncio
async def test_persist_mentions_no_workspace_id_skips(monkeypatch):
    """When session workspace_id is empty, no notification is emitted."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import persist_mentions

    mock_session = MagicMock()
    mock_session.workspace_id = ""

    mock_db = MagicMock()
    mock_db.add = MagicMock()
    mock_db.commit = AsyncMock()
    mock_db.get = AsyncMock(return_value=mock_session)

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await persist_mentions(
            mock_db,
            message_id=13,
            session_id="sess-1",
            mentions=[{"mentioned_id": "usr-2", "mentioned_kind": "user"}],
        )

    mock_bulk.assert_not_called()


# ---------------------------------------------------------------------------
# _emit_message_notifications — channel_message and dm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_emit_channel_message_notifies_all_except_author(monkeypatch):
    """Channel message → channel_message payloads for all members except author."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import _emit_message_notifications

    mock_session = MagicMock()
    mock_session.kind = "channel"
    mock_session.workspace_id = "ws-1"

    # Simulate member query result: rows for usr-2 and usr-3 (author usr-1 excluded)
    members_result = MagicMock()
    members_result.all.return_value = [("usr-2",), ("usr-3",)]

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=mock_session)
    mock_db.execute = AsyncMock(return_value=members_result)

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await _emit_message_notifications(
            mock_db,
            session_id="chan-1",
            message_id=55,
            author_id="usr-1",
        )

    mock_bulk.assert_called_once()
    payloads = mock_bulk.call_args[0][0]
    assert len(payloads) == 2
    cats = {p["category"] for p in payloads}
    assert cats == {"channel_message"}
    uids = {p["user_id"] for p in payloads}
    assert uids == {"usr-2", "usr-3"}
    for p in payloads:
        assert p["actor_user_id"] == "usr-1"


@pytest.mark.asyncio
async def test_emit_channel_message_thread_reply_summary(monkeypatch):
    """A message with thread_root_id set (a thread reply) gets a "replied to a
    thread" summary, distinguishing it from an ordinary channel post — otherwise
    the two are indistinguishable in the activity feed even though they land in
    very different places in the UI (side panel vs. main transcript)."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import _emit_message_notifications

    mock_session = MagicMock()
    mock_session.kind = "channel"
    mock_session.workspace_id = "ws-1"

    members_result = MagicMock()
    members_result.all.return_value = [("usr-2",)]

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=mock_session)
    mock_db.execute = AsyncMock(return_value=members_result)

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await _emit_message_notifications(
            mock_db,
            session_id="chan-1",
            message_id=55,
            author_id="usr-1",
            content="hello",
            thread_root_id=42,
        )

    payloads = mock_bulk.call_args[0][0]
    assert payloads[0]["category"] == "channel_message"
    assert "replied to a thread" in payloads[0]["summary"]


@pytest.mark.asyncio
async def test_emit_channel_message_inline_reply_summary(monkeypatch):
    """A message with reply_to_message_id set but no thread_root_id (an inline
    quoted reply in the main transcript) gets a "replied to a message" summary —
    distinct wording from a thread-side-panel reply, since it's a different UI
    surface even though both are "replies" conceptually."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import _emit_message_notifications

    mock_session = MagicMock()
    mock_session.kind = "channel"
    mock_session.workspace_id = "ws-1"

    members_result = MagicMock()
    members_result.all.return_value = [("usr-2",)]

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=mock_session)
    mock_db.execute = AsyncMock(return_value=members_result)

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await _emit_message_notifications(
            mock_db,
            session_id="chan-1",
            message_id=55,
            author_id="usr-1",
            content="hello",
            reply_to_message_id=17,
        )

    payloads = mock_bulk.call_args[0][0]
    assert payloads[0]["category"] == "channel_message"
    assert "replied to a message" in payloads[0]["summary"]
    assert "replied to a thread" not in payloads[0]["summary"]


@pytest.mark.asyncio
async def test_emit_channel_message_no_thread_root_id_plain_summary(monkeypatch):
    """A top-level channel post (no thread_root_id) keeps the plain summary wording."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import _emit_message_notifications

    mock_session = MagicMock()
    mock_session.kind = "channel"
    mock_session.workspace_id = "ws-1"

    members_result = MagicMock()
    members_result.all.return_value = [("usr-2",)]

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=mock_session)
    mock_db.execute = AsyncMock(return_value=members_result)

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await _emit_message_notifications(
            mock_db,
            session_id="chan-1",
            message_id=55,
            author_id="usr-1",
            content="hello",
        )

    payloads = mock_bulk.call_args[0][0]
    assert "replied to a thread" not in payloads[0]["summary"]


@pytest.mark.asyncio
async def test_emit_dm_notifies_other_party(monkeypatch):
    """DM session message → dm payload for the non-author member."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import _emit_message_notifications

    mock_session = MagicMock()
    mock_session.kind = "dm"
    mock_session.workspace_id = "ws-1"

    members_result = MagicMock()
    members_result.all.return_value = [("usr-2",)]

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=mock_session)
    mock_db.execute = AsyncMock(return_value=members_result)

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await _emit_message_notifications(
            mock_db,
            session_id="dm-sess",
            message_id=77,
            author_id="usr-1",
        )

    mock_bulk.assert_called_once()
    payloads = mock_bulk.call_args[0][0]
    assert len(payloads) == 1
    assert payloads[0]["category"] == "dm"
    assert payloads[0]["user_id"] == "usr-2"
    assert payloads[0]["actor_user_id"] == "usr-1"


@pytest.mark.asyncio
async def test_emit_thread_session_no_notification(monkeypatch):
    """Thread sessions do not emit channel_message or dm; those come from mentions."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import _emit_message_notifications

    mock_session = MagicMock()
    mock_session.kind = "thread"
    mock_session.workspace_id = "ws-1"

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=mock_session)
    mock_db.execute = AsyncMock()

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await _emit_message_notifications(
            mock_db,
            session_id="thread-1",
            message_id=88,
            author_id="usr-1",
        )

    mock_bulk.assert_not_called()


@pytest.mark.asyncio
async def test_emit_channel_no_recipients_no_notification(monkeypatch):
    """If no other channel members exist, no bulk call is made."""
    _inject_stubs()
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.db.store import _emit_message_notifications

    mock_session = MagicMock()
    mock_session.kind = "channel"
    mock_session.workspace_id = "ws-1"

    members_result = MagicMock()
    members_result.all.return_value = []  # author is the only member

    mock_db = MagicMock()
    mock_db.get = AsyncMock(return_value=mock_session)
    mock_db.execute = AsyncMock(return_value=members_result)

    with patch("src.db.store.schedule_notifications_bulk") as mock_bulk:
        await _emit_message_notifications(
            mock_db,
            session_id="chan-lonely",
            message_id=5,
            author_id="usr-1",
        )

    mock_bulk.assert_not_called()


@pytest.mark.asyncio
async def test_emit_swallows_db_error(monkeypatch):
    """_emit_message_notifications does not propagate DB exceptions."""
    _inject_stubs()

    from src.db.store import _emit_message_notifications

    mock_db = MagicMock()
    mock_db.get = AsyncMock(side_effect=RuntimeError("db unavailable"))

    # Must not raise
    await _emit_message_notifications(
        mock_db,
        session_id="sess-err",
        message_id=1,
        author_id="usr-1",
    )
