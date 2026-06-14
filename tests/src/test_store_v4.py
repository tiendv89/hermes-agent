"""Unit tests for v4 team-chat store functions (store_v4.py).

Covers the T1 test plan:
  - Member add / remove / list
  - Member-scoped session listing (own ∪ member-of; excludes channels)
  - Mention persistence and resolution
  - kind='channel' session creation (create_channel)
  - Unique channel name enforcement
  - Hard-delete cascades the channel's messages
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

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


# ---------------------------------------------------------------------------
# Member store tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_member_new():
    """add_member inserts a SessionMember row when none exists."""
    from src.db.store_v4 import add_member

    db = _mock_db()
    db.get = AsyncMock(return_value=None)

    await add_member(db, "sess_1", "user_a", added_by="user_b")

    db.add.assert_called_once()
    added = db.add.call_args[0][0]
    assert added.session_id == "sess_1"
    assert added.user_id == "user_a"
    assert added.added_by == "user_b"
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_add_member_idempotent():
    """add_member is a no-op when the member already exists."""
    from src.db.store_v4 import add_member
    from src.db.models import SessionMember

    existing = MagicMock(spec=SessionMember)
    db = _mock_db()
    db.get = AsyncMock(return_value=existing)

    await add_member(db, "sess_1", "user_a", added_by="user_b")

    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_remove_member():
    """remove_member executes a delete and commits."""
    from src.db.store_v4 import remove_member

    db = _mock_db()
    await remove_member(db, "sess_1", "user_a")

    db.execute.assert_called_once()
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_list_members_returns_ordered_list():
    """list_members returns members ordered by added_at."""
    from src.db.store_v4 import list_members

    now = time.time()
    m1 = MagicMock(user_id="u1", role_label="PO", added_by="owner", added_at=now - 100)
    m2 = MagicMock(user_id="u2", role_label=None, added_by="owner", added_at=now - 50)

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [m1, m2]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    members = await list_members(db, "sess_1")

    assert len(members) == 2
    assert members[0]["user_id"] == "u1"
    assert members[0]["role_label"] == "PO"
    assert members[1]["user_id"] == "u2"
    assert members[1]["role_label"] is None


@pytest.mark.asyncio
async def test_list_member_sessions_scope():
    """list_member_sessions returns own ∪ member-of sessions, not channels."""
    from src.db.store_v4 import list_member_sessions

    now = time.time()
    row_owned = MagicMock(
        id="sess_own",
        title="My Session",
        feature_id="feat-1",
        started_at=now - 200,
        last_active_at=now - 10,
        model="claude-sonnet",
        kind="thread",
    )
    row_member_of = MagicMock(
        id="sess_member",
        title="Team Session",
        feature_id="feat-2",
        started_at=now - 300,
        last_active_at=now - 50,
        model=None,
        kind="thread",
    )

    result_mock = MagicMock()
    result_mock.all.return_value = [row_owned, row_member_of]

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    sessions = await list_member_sessions(db, "ws-1", "user_a")

    assert len(sessions) == 2
    assert sessions[0]["id"] == "sess_own"
    assert sessions[1]["id"] == "sess_member"
    assert sessions[0]["kind"] == "thread"


@pytest.mark.asyncio
async def test_is_member_true():
    """is_member returns True when the membership row exists."""
    from src.db.store_v4 import is_member
    from src.db.models import SessionMember

    db = _mock_db()
    db.get = AsyncMock(return_value=MagicMock(spec=SessionMember))

    assert await is_member(db, "sess_1", "user_a") is True


@pytest.mark.asyncio
async def test_is_member_false():
    """is_member returns False when the membership row is absent."""
    from src.db.store_v4 import is_member

    db = _mock_db()
    db.get = AsyncMock(return_value=None)

    assert await is_member(db, "sess_1", "user_x") is False


# ---------------------------------------------------------------------------
# Mention store tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_mentions_inserts_rows():
    """persist_mentions inserts one MessageMention row per mention."""
    from src.db.store_v4 import persist_mentions

    db = _mock_db()
    mentions = [
        {"mentioned_id": "user_a", "mentioned_kind": "user"},
        {"mentioned_id": "agent", "mentioned_kind": "agent"},
    ]

    await persist_mentions(db, message_id=42, session_id="sess_1", mentions=mentions)

    assert db.add.call_count == 2
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_persist_mentions_empty_list():
    """persist_mentions with an empty list does not commit."""
    from src.db.store_v4 import persist_mentions

    db = _mock_db()
    await persist_mentions(db, message_id=42, session_id="sess_1", mentions=[])

    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_mentions_returns_list():
    """resolve_mentions returns all mention rows for a message."""
    from src.db.store_v4 import resolve_mentions

    m = MagicMock(
        id=1, message_id=42, session_id="sess_1",
        mentioned_id="user_a", mentioned_kind="user", read_at=None,
    )

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [m]
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    mentions = await resolve_mentions(db, message_id=42)

    assert len(mentions) == 1
    assert mentions[0]["mentioned_id"] == "user_a"
    assert mentions[0]["mentioned_kind"] == "user"


@pytest.mark.asyncio
async def test_get_unread_mention_count():
    """get_unread_mention_count returns the scalar count."""
    from src.db.store_v4 import get_unread_mention_count

    result_mock = MagicMock()
    result_mock.scalar_one.return_value = 3

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    count = await get_unread_mention_count(db, "sess_1", "user_a")
    assert count == 3


@pytest.mark.asyncio
async def test_get_unread_mention_count_zero_when_none():
    """get_unread_mention_count returns 0 when scalar is None."""
    from src.db.store_v4 import get_unread_mention_count

    result_mock = MagicMock()
    result_mock.scalar_one.return_value = None

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    count = await get_unread_mention_count(db, "sess_1", "user_a")
    assert count == 0


@pytest.mark.asyncio
async def test_mark_mentions_read():
    """mark_mentions_read executes an update and commits."""
    from src.db.store_v4 import mark_mentions_read

    db = _mock_db()
    await mark_mentions_read(db, "sess_1", "user_a")

    db.execute.assert_called_once()
    db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Channel store tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_channel_returns_session_id():
    """create_channel creates a kind='channel' session and returns its id."""
    from src.db.store_v4 import create_channel
    from src.db.models import Session, SessionMember

    # Capture the Session object added to track its attributes
    added_objects = []

    def _fake_add(obj):
        if isinstance(obj, Session):
            obj.id = "sess_chan_abc"
        added_objects.append(obj)

    db = _mock_db()
    db.add = MagicMock(side_effect=_fake_add)

    channel_id = await create_channel(
        db,
        workspace_id="ws-1",
        name="#general",
        creator_user_id="user_a",
        description="General discussion",
    )

    assert channel_id == "sess_chan_abc"

    sessions = [o for o in added_objects if isinstance(o, Session)]
    members = [o for o in added_objects if isinstance(o, SessionMember)]

    assert len(sessions) == 1
    assert sessions[0].kind == "channel"
    assert sessions[0].feature_id == ""
    assert sessions[0].title == "#general"
    assert sessions[0].user_id == "user_a"

    assert len(members) == 1
    assert members[0].user_id == "user_a"

    db.flush.assert_called_once()
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_create_channel_duplicate_name_raises():
    """create_channel propagates IntegrityError on duplicate channel name."""
    from sqlalchemy.exc import IntegrityError
    from src.db.store_v4 import create_channel

    db = _mock_db()
    db.flush = AsyncMock(side_effect=IntegrityError("unique", {}, None))

    with pytest.raises(IntegrityError):
        await create_channel(db, "ws-1", "#general", "user_a")


@pytest.mark.asyncio
async def test_create_channel_without_description():
    """create_channel works without an optional description."""
    from src.db.store_v4 import create_channel
    from src.db.models import Session

    added_sessions = []

    def _fake_add(obj):
        if isinstance(obj, Session):
            obj.id = "sess_nodesc"
            added_sessions.append(obj)

    db = _mock_db()
    db.add = MagicMock(side_effect=_fake_add)

    channel_id = await create_channel(db, "ws-1", "#random", "user_b")

    assert channel_id == "sess_nodesc"
    assert added_sessions[0].extra == {}


@pytest.mark.asyncio
async def test_list_channels_returns_non_archived():
    """list_channels returns non-archived channels ordered by started_at DESC."""
    from src.db.store_v4 import list_channels

    now = time.time()
    r1 = MagicMock(
        id="sess_ch1", title="#general", user_id="user_a",
        started_at=now - 100, last_active_at=now - 10, extra={},
    )
    r2 = MagicMock(
        id="sess_ch2", title="#design", user_id="user_b",
        started_at=now - 200, last_active_at=now - 20, extra={"description": "UX"},
    )

    result_mock = MagicMock()
    result_mock.all.return_value = [r1, r2]

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    channels = await list_channels(db, "ws-1")

    assert len(channels) == 2
    assert channels[0]["name"] == "#general"
    assert channels[1]["description"] == "UX"


@pytest.mark.asyncio
async def test_get_channel_found():
    """get_channel returns the session for a valid channel id."""
    from src.db.store_v4 import get_channel
    from src.db.models import Session

    fake_session = MagicMock(spec=Session)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = fake_session

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    sess = await get_channel(db, "sess_ch1")
    assert sess is fake_session


@pytest.mark.asyncio
async def test_get_channel_not_found():
    """get_channel returns None for an unknown or non-channel session id."""
    from src.db.store_v4 import get_channel

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    sess = await get_channel(db, "sess_nope")
    assert sess is None


@pytest.mark.asyncio
async def test_hard_delete_channel_existing():
    """hard_delete_channel deletes the channel session and returns True."""
    from src.db.store_v4 import hard_delete_channel
    from src.db.models import Session

    fake_session = MagicMock(spec=Session)
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = fake_session

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    deleted = await hard_delete_channel(db, "sess_ch1")

    assert deleted is True
    db.delete.assert_called_once_with(fake_session)
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_hard_delete_channel_not_found():
    """hard_delete_channel returns False when the channel does not exist."""
    from src.db.store_v4 import hard_delete_channel

    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    deleted = await hard_delete_channel(db, "sess_nope")

    assert deleted is False
    db.delete.assert_not_called()
    db.commit.assert_not_called()


# ---------------------------------------------------------------------------
# Model smoke test — kind and author_id columns present
# ---------------------------------------------------------------------------


def test_session_model_has_kind_column():
    """Session.kind column is declared on the model."""
    from src.db.models import Session
    assert hasattr(Session, "kind")
    assert Session.__table__.columns["kind"].default.arg == "thread"


def test_message_model_has_author_id_column():
    """Message.author_id column is declared on the model (nullable for legacy rows)."""
    from src.db.models import Message
    assert hasattr(Message, "author_id")
    assert Message.__table__.columns["author_id"].nullable is True


def test_session_member_model():
    """SessionMember model has expected columns."""
    from src.db.models import SessionMember
    cols = SessionMember.__table__.columns
    assert "session_id" in cols
    assert "user_id" in cols
    assert "role_label" in cols
    assert "added_by" in cols
    assert "added_at" in cols


def test_message_mention_model():
    """MessageMention model has expected columns."""
    from src.db.models import MessageMention
    cols = MessageMention.__table__.columns
    assert "message_id" in cols
    assert "session_id" in cols
    assert "mentioned_id" in cols
    assert "mentioned_kind" in cols
    assert "read_at" in cols
