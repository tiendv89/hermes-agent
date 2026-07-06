"""Unit tests for get_feature_participants in src/db/store.py.

Used to fan out feature-lifecycle (stage approval) notifications to everyone
who's engaged with a feature — session owners and explicit members alike,
across every channel/DM/thread scoped to that feature.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


def _mock_db():
    db = MagicMock()
    db.execute = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_get_feature_participants_unions_owners_and_members():
    from src.db.store import get_feature_participants

    db = _mock_db()

    sessions_result = MagicMock()
    sessions_result.all.return_value = [
        ("sess-1", "owner-a"),
        ("sess-2", "owner-b"),
    ]
    members_result = MagicMock()
    members_result.all.return_value = [("member-c",), ("owner-a",)]

    db.execute = AsyncMock(side_effect=[sessions_result, members_result])

    participants = await get_feature_participants(db, "ws-1", "feat-1")

    assert participants == {"owner-a", "owner-b", "member-c"}


@pytest.mark.asyncio
async def test_get_feature_participants_no_sessions_skips_member_query():
    from src.db.store import get_feature_participants

    db = _mock_db()
    sessions_result = MagicMock()
    sessions_result.all.return_value = []
    db.execute = AsyncMock(return_value=sessions_result)

    participants = await get_feature_participants(db, "ws-1", "feat-empty")

    assert participants == set()
    db.execute.assert_called_once()


@pytest.mark.asyncio
async def test_get_feature_participants_ignores_sessions_with_no_owner():
    from src.db.store import get_feature_participants

    db = _mock_db()
    sessions_result = MagicMock()
    sessions_result.all.return_value = [("sess-1", None)]
    members_result = MagicMock()
    members_result.all.return_value = [("member-x",)]

    db.execute = AsyncMock(side_effect=[sessions_result, members_result])

    participants = await get_feature_participants(db, "ws-1", "feat-1")

    assert participants == {"member-x"}
