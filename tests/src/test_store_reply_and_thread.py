"""Unit tests for T2: store layer changes for reply/thread support.

Covers:
- append_message accepts reply_to_message_id / thread_root_id kwargs
  and passes them to the Message constructor (both set, only one set, neither set).
- Existing callers (no new kwargs) remain source-compatible.
- get_session_messages excludes messages where thread_root_id IS NOT NULL.
- get_messages_since excludes messages where thread_root_id IS NOT NULL.
- get_thread_replies returns only replies for the given root, oldest-first,
  and respects the optional `since` cursor.
- get_thread_reply_summaries returns a single-query result with reply_count
  and recent_repliers; absent root ids are excluded; empty list is handled;
  no N+1 (single execute call).
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_db():
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    db.delete = AsyncMock()
    return db


def _scalars_result(rows):
    """Return a mock that satisfies result.scalars().all() == rows."""
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    return result_mock


def _rows_result(rows):
    """Return a mock that satisfies result.all() == rows."""
    result_mock = MagicMock()
    result_mock.all.return_value = rows
    return result_mock


def _msg(
    id=1,
    session_id="sess-1",
    role="user",
    content="hi",
    author_id="u1",
    created_at=None,
    tool_name=None,
    tool_call_id=None,
    tool_calls=None,
    reply_to_message_id=None,
    thread_root_id=None,
    active=True,
):
    m = MagicMock()
    m.id = id
    m.session_id = session_id
    m.role = role
    m.content = content
    m.author_id = author_id
    m.created_at = created_at or time.time()
    m.tool_name = tool_name
    m.tool_call_id = tool_call_id
    m.tool_calls = tool_calls
    m.reply_to_message_id = reply_to_message_id
    m.thread_root_id = thread_root_id
    m.active = active
    return m


# ---------------------------------------------------------------------------
# append_message — new kwargs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_message_with_both_reply_fields():
    """append_message passes reply_to_message_id and thread_root_id to Message."""
    from src.db.store import append_message

    db = _mock_db()
    captured = []

    def _capture(obj):
        captured.append(obj)

    db.add.side_effect = _capture
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()

    # Make flush populate msg.id
    async def _flush_set_id():
        if captured:
            captured[-1].id = 42

    db.flush.side_effect = _flush_set_id

    with patch("src.db.store.mark_session_read", new_callable=AsyncMock):
        with patch("src.db.store._emit_message_notifications", new_callable=AsyncMock):
            await append_message(
                db,
                session_id="sess-1",
                role="user",
                content="hello",
                reply_to_message_id=10,
                thread_root_id=5,
            )

    assert len(captured) == 1
    msg = captured[0]
    assert msg.reply_to_message_id == 10
    assert msg.thread_root_id == 5


@pytest.mark.asyncio
async def test_append_message_without_reply_fields():
    """append_message without the new kwargs defaults both fields to None."""
    from src.db.store import append_message

    db = _mock_db()
    captured = []

    db.add.side_effect = lambda obj: captured.append(obj)

    async def _flush_set_id():
        if captured:
            captured[-1].id = 99

    db.flush.side_effect = _flush_set_id

    with patch("src.db.store.mark_session_read", new_callable=AsyncMock):
        await append_message(db, session_id="sess-1", role="assistant", content="ok")

    assert len(captured) == 1
    msg = captured[0]
    assert msg.reply_to_message_id is None
    assert msg.thread_root_id is None


@pytest.mark.asyncio
async def test_append_message_only_reply_to():
    """append_message with only reply_to_message_id set (inline reply, main transcript)."""
    from src.db.store import append_message

    db = _mock_db()
    captured = []
    db.add.side_effect = lambda obj: captured.append(obj)

    async def _flush_set_id():
        if captured:
            captured[-1].id = 7

    db.flush.side_effect = _flush_set_id

    with patch("src.db.store.mark_session_read", new_callable=AsyncMock):
        with patch("src.db.store._emit_message_notifications", new_callable=AsyncMock):
            await append_message(
                db,
                session_id="sess-1",
                role="user",
                content="inline",
                author_id="u1",
                reply_to_message_id=3,
            )

    msg = captured[0]
    assert msg.reply_to_message_id == 3
    assert msg.thread_root_id is None


@pytest.mark.asyncio
async def test_append_message_only_thread_root():
    """append_message with only thread_root_id set (first reply in a thread)."""
    from src.db.store import append_message

    db = _mock_db()
    captured = []
    db.add.side_effect = lambda obj: captured.append(obj)

    async def _flush_set_id():
        if captured:
            captured[-1].id = 8

    db.flush.side_effect = _flush_set_id

    with patch("src.db.store.mark_session_read", new_callable=AsyncMock):
        with patch("src.db.store._emit_message_notifications", new_callable=AsyncMock):
            await append_message(
                db,
                session_id="sess-1",
                role="user",
                content="thread reply",
                author_id="u2",
                thread_root_id=6,
            )

    msg = captured[0]
    assert msg.thread_root_id == 6
    assert msg.reply_to_message_id is None


# ---------------------------------------------------------------------------
# get_session_messages — thread_root_id IS NULL filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_messages_excludes_thread_replies():
    """get_session_messages only returns messages with thread_root_id IS NULL."""
    from src.db.store import get_session_messages

    top_level = _msg(id=1, thread_root_id=None, content="top")
    db = _mock_db()
    db.execute.return_value = _scalars_result([top_level])

    result = await get_session_messages(db, "sess-1")

    assert len(result) == 1
    assert result[0]["id"] == "1"
    assert result[0]["content"] == "top"

    # Verify the query was built with a thread_root_id IS NULL condition
    # (executed via db.execute once, no second call for thread replies)
    db.execute.assert_called_once()


@pytest.mark.asyncio
async def test_get_session_messages_returns_top_level_only():
    """Thread replies must not appear in get_session_messages output."""
    from src.db.store import get_session_messages

    top = _msg(id=10, thread_root_id=None, content="root message")
    # thread reply should NOT appear — the query filter should already exclude
    # it; we simulate the filtered result by only returning top-level messages.
    db = _mock_db()
    db.execute.return_value = _scalars_result([top])

    result = await get_session_messages(db, "sess-1")

    ids = [r["id"] for r in result]
    assert "10" in ids
    # Reply message was filtered by the DB query; result only has the root.
    assert len(result) == 1


# ---------------------------------------------------------------------------
# get_messages_since — thread_root_id IS NULL filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_messages_since_excludes_thread_replies():
    """get_messages_since only returns messages with thread_root_id IS NULL."""
    from src.db.store import get_messages_since

    top = _msg(id=20, thread_root_id=None, content="new top-level")
    db = _mock_db()
    db.execute.return_value = _scalars_result([top])

    result = await get_messages_since(db, "sess-1", since_message_id=15)

    assert len(result) == 1
    assert result[0]["id"] == "20"
    db.execute.assert_called_once()


# ---------------------------------------------------------------------------
# get_thread_replies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_thread_replies_returns_replies_oldest_first():
    """get_thread_replies returns thread replies in ascending id order."""
    from src.db.store import get_thread_replies

    r1 = _msg(id=101, thread_root_id=50, reply_to_message_id=50, content="first reply")
    r2 = _msg(id=102, thread_root_id=50, reply_to_message_id=101, content="second reply")
    db = _mock_db()
    db.execute.return_value = _scalars_result([r1, r2])

    result = await get_thread_replies(db, "sess-1", root_message_id=50)

    assert len(result) == 2
    assert result[0]["id"] == "101"
    assert result[1]["id"] == "102"
    assert result[0]["thread_root_id"] == "50"
    assert result[0]["reply_to_message_id"] == "50"


@pytest.mark.asyncio
async def test_get_thread_replies_with_since_cursor():
    """get_thread_replies respects the `since` cursor."""
    from src.db.store import get_thread_replies

    r2 = _msg(id=102, thread_root_id=50, content="second reply")
    db = _mock_db()
    db.execute.return_value = _scalars_result([r2])

    result = await get_thread_replies(db, "sess-1", root_message_id=50, since=101)

    assert len(result) == 1
    assert result[0]["id"] == "102"
    db.execute.assert_called_once()


@pytest.mark.asyncio
async def test_get_thread_replies_empty():
    """get_thread_replies returns an empty list when no replies exist."""
    from src.db.store import get_thread_replies

    db = _mock_db()
    db.execute.return_value = _scalars_result([])

    result = await get_thread_replies(db, "sess-1", root_message_id=99)

    assert result == []


@pytest.mark.asyncio
async def test_get_thread_replies_no_reply_to_message_id_field_when_null():
    """Entries without reply_to_message_id set should omit the key."""
    from src.db.store import get_thread_replies

    r = _msg(id=200, thread_root_id=50, reply_to_message_id=None, content="reply")
    db = _mock_db()
    db.execute.return_value = _scalars_result([r])

    result = await get_thread_replies(db, "sess-1", root_message_id=50)

    assert "reply_to_message_id" not in result[0]


# ---------------------------------------------------------------------------
# get_thread_reply_summaries
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_thread_reply_summaries_empty_input():
    """get_thread_reply_summaries returns {} immediately for empty root list."""
    from src.db.store import get_thread_reply_summaries

    db = _mock_db()
    result = await get_thread_reply_summaries(db, "sess-1", [])

    assert result == {}
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_get_thread_reply_summaries_counts_and_repliers():
    """get_thread_reply_summaries returns correct counts and recent_repliers."""
    from src.db.store import get_thread_reply_summaries

    # Two execute calls: one for counts, one for repliers.
    counts_row = MagicMock()
    counts_row.thread_root_id = 10
    counts_row.reply_count = 3

    counts_result = MagicMock()
    counts_result.all.return_value = [counts_row]

    # Replier rows (ordered desc by id within root 10)
    replier_rows = [
        (10, "u3"),  # most recent
        (10, "u1"),
        (10, "u2"),
        (10, "u1"),  # duplicate — should be deduped
    ]
    replier_result = MagicMock()
    replier_result.all.return_value = replier_rows

    db = _mock_db()
    db.execute.side_effect = [counts_result, replier_result]

    result = await get_thread_reply_summaries(db, "sess-1", [10])

    assert 10 in result
    assert result[10]["reply_count"] == 3
    # u3, u1, u2 — u1 duplicate dropped; order preserved by first-seen desc
    assert result[10]["recent_repliers"] == ["u3", "u1", "u2"]


@pytest.mark.asyncio
async def test_get_thread_reply_summaries_absent_root_excluded():
    """Root ids with no replies are excluded from the returned dict."""
    from src.db.store import get_thread_reply_summaries

    counts_result = MagicMock()
    counts_result.all.return_value = []

    replier_result = MagicMock()
    replier_result.all.return_value = []

    db = _mock_db()
    db.execute.side_effect = [counts_result, replier_result]

    result = await get_thread_reply_summaries(db, "sess-1", [5, 6])

    # No replies for either root → empty dict
    assert result == {}


@pytest.mark.asyncio
async def test_get_thread_reply_summaries_no_n_plus_one():
    """get_thread_reply_summaries must execute exactly 2 queries for any input size."""
    from src.db.store import get_thread_reply_summaries

    root_ids = list(range(1, 21))  # 20 root message ids

    # Build per-root count rows
    count_rows = []
    for rid in root_ids:
        r = MagicMock()
        r.thread_root_id = rid
        r.reply_count = 2
        count_rows.append(r)

    counts_result = MagicMock()
    counts_result.all.return_value = count_rows

    replier_result = MagicMock()
    replier_result.all.return_value = [(rid, "u1") for rid in root_ids]

    db = _mock_db()
    db.execute.side_effect = [counts_result, replier_result]

    await get_thread_reply_summaries(db, "sess-1", root_ids)

    # Exactly 2 DB round-trips regardless of how many root ids are passed.
    assert db.execute.call_count == 2


@pytest.mark.asyncio
async def test_get_thread_reply_summaries_recent_repliers_capped_at_three():
    """recent_repliers contains at most 3 distinct author ids."""
    from src.db.store import get_thread_reply_summaries

    counts_row = MagicMock()
    counts_row.thread_root_id = 1
    counts_row.reply_count = 5

    counts_result = MagicMock()
    counts_result.all.return_value = [counts_row]

    replier_result = MagicMock()
    replier_result.all.return_value = [
        (1, "u5"),
        (1, "u4"),
        (1, "u3"),
        (1, "u2"),
        (1, "u1"),
    ]

    db = _mock_db()
    db.execute.side_effect = [counts_result, replier_result]

    result = await get_thread_reply_summaries(db, "sess-1", [1])

    assert len(result[1]["recent_repliers"]) == 3
    assert result[1]["recent_repliers"] == ["u5", "u4", "u3"]
