"""Unit tests for the general unread-message-count store functions in store.py:

- mark_session_read: creates or updates a SessionRead cursor
- get_unread_message_counts_by_session: computes unread = message_count - last_read
- append_message: auto-advances the author's own cursor so their own message
  never shows up as unread to themselves
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _mock_db():
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_mark_session_read_creates_row_when_none_exists():
    from src.db.store import mark_session_read

    db = _mock_db()
    mock_session = MagicMock()
    mock_session.message_count = 7

    # First .get() call resolves the Session; second resolves SessionRead (None).
    db.get = AsyncMock(side_effect=[mock_session, None])

    await mark_session_read(db, "sess-1", "user-1")

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.session_id == "sess-1"
    assert added.user_id == "user-1"
    assert added.last_read_message_count == 7
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_mark_session_read_updates_existing_row():
    from src.db.store import mark_session_read

    db = _mock_db()
    mock_session = MagicMock()
    mock_session.message_count = 12
    existing_read = MagicMock()
    existing_read.last_read_message_count = 3

    db.get = AsyncMock(side_effect=[mock_session, existing_read])

    await mark_session_read(db, "sess-1", "user-1")

    db.add.assert_not_called()
    assert existing_read.last_read_message_count == 12
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_mark_session_read_noop_when_session_missing():
    from src.db.store import mark_session_read

    db = _mock_db()
    db.get = AsyncMock(return_value=None)

    await mark_session_read(db, "sess-missing", "user-1")

    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_get_unread_message_counts_by_session_computes_diff():
    from src.db.store import get_unread_message_counts_by_session

    db = _mock_db()
    result = MagicMock()
    # (session_id, message_count, last_read_message_count)
    result.all.return_value = [
        ("chan-unread", 10, 4),  # 6 unread
        ("chan-caught-up", 5, 5),  # 0 unread -> excluded
        ("chan-never-read", 3, None),  # last_read defaults to 0 -> 3 unread
    ]
    db.execute = AsyncMock(return_value=result)

    counts = await get_unread_message_counts_by_session(db, "ws-1", "user-1")

    assert counts == {"chan-unread": 6, "chan-never-read": 3}
    assert "chan-caught-up" not in counts


@pytest.mark.asyncio
async def test_append_message_advances_own_cursor_on_send(monkeypatch):
    """A human-authored message advances the author's own read cursor so it
    never appears as unread to them."""
    import sys
    import types

    if "run_agent" not in sys.modules:
        stub = types.ModuleType("run_agent")
        stub.AIAgent = MagicMock()
        sys.modules["run_agent"] = stub

    from src.db import store

    db = _mock_db()
    msg = MagicMock()
    msg.id = 42

    calls: list = []

    async def _fake_mark_session_read(db_, session_id, user_id):
        calls.append((session_id, user_id))

    async def _fake_emit(*args, **kwargs):
        pass

    monkeypatch.setattr(store, "mark_session_read", _fake_mark_session_read)
    monkeypatch.setattr(store, "_emit_message_notifications", _fake_emit)

    def _add(obj):
        obj.id = 42

    db.add = MagicMock(side_effect=_add)

    await store.append_message(
        db,
        session_id="sess-1",
        role="user",
        content="hello",
        author_id="user-1",
    )

    assert ("sess-1", "user-1") in calls
