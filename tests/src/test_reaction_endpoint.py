"""Tests for T3 (m3-agent-chat-essential-feature): reaction toggle endpoint.

Covers:
- toggle_message_reaction: insert when absent, delete when present (toggling).
- Aggregate response shape matches {emoji, count, reactedByMe}[].
- get_reactions_for_messages: bulk query; empty list for no reactions.
- get_session_messages: embeds reactions per message (no N+1).
- POST /messages/{message_id}/reactions: 200 toggle, 404 missing, 400 bad emoji/id.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


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


def _row(message_id, emoji, cnt, reacted_by_me):
    r = MagicMock()
    r.message_id = message_id
    r.emoji = emoji
    r.cnt = cnt
    r.reacted_by_me = reacted_by_me
    return r


# ---------------------------------------------------------------------------
# get_reactions_for_messages — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_reactions_for_messages_empty_ids():
    """Empty message_ids returns empty dict without a DB query."""
    from src.db.store import get_reactions_for_messages

    db = _mock_db()
    result = await get_reactions_for_messages(db, [], user_id="user-1")

    assert result == {}
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_get_reactions_for_messages_returns_grouped_by_message():
    """Rows from the DB are grouped by message_id."""
    from src.db.store import get_reactions_for_messages

    rows = [
        _row(1, "👀", 2, True),
        _row(1, "✅", 1, False),
        _row(2, "🙌", 3, True),
    ]
    result_mock = MagicMock()
    result_mock.all.return_value = rows

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_reactions_for_messages(db, [1, 2], user_id="user-1")

    assert set(result.keys()) == {1, 2}
    assert len(result[1]) == 2
    assert len(result[2]) == 1

    emojis_msg1 = {r["emoji"] for r in result[1]}
    assert emojis_msg1 == {"👀", "✅"}

    msg2_reaction = result[2][0]
    assert msg2_reaction["emoji"] == "🙌"
    assert msg2_reaction["count"] == 3
    assert msg2_reaction["reactedByMe"] is True


@pytest.mark.asyncio
async def test_get_reactions_for_messages_no_reactions_absent():
    """A message with no reactions is absent from the result dict."""
    from src.db.store import get_reactions_for_messages

    result_mock = MagicMock()
    result_mock.all.return_value = []

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_reactions_for_messages(db, [99], user_id="user-1")

    assert result == {}


@pytest.mark.asyncio
async def test_get_reactions_reacted_by_me_false():
    """reactedByMe is False when user has not reacted."""
    from src.db.store import get_reactions_for_messages

    rows = [_row(5, "✅", 1, False)]
    result_mock = MagicMock()
    result_mock.all.return_value = rows

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    result = await get_reactions_for_messages(db, [5], user_id="other-user")

    assert result[5][0]["reactedByMe"] is False


# ---------------------------------------------------------------------------
# toggle_message_reaction — unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_toggle_adds_reaction_when_absent():
    """When the reaction row is absent, INSERT ... ON CONFLICT DO NOTHING inserts it."""
    from src.db.store import toggle_message_reaction

    # First execute: pg_insert ... ON CONFLICT DO NOTHING → rowcount=1 (inserted).
    insert_result = MagicMock()
    insert_result.rowcount = 1

    # Second execute: aggregate after insert.
    aggregate_result = MagicMock()
    aggregate_result.all.return_value = [_row(1, "👀", 1, True)]

    db = _mock_db()
    db.execute = AsyncMock(side_effect=[insert_result, aggregate_result])

    reactions = await toggle_message_reaction(db, 1, "user-1", "👀")

    # INSERT path: no db.add, two execute calls (insert + aggregate).
    db.add.assert_not_called()
    assert db.execute.call_count == 2
    db.commit.assert_called()
    assert reactions == [{"emoji": "👀", "count": 1, "reactedByMe": True, "userIds": []}]


@pytest.mark.asyncio
async def test_toggle_removes_reaction_when_present():
    """When the reaction row exists, INSERT conflicts (rowcount=0) and a DELETE is issued."""
    from src.db.store import toggle_message_reaction

    # First execute: INSERT conflicts → rowcount=0 (row already existed).
    insert_result = MagicMock()
    insert_result.rowcount = 0

    # Second execute: DELETE the existing row via execute(delete(...).where(...)).
    delete_result = MagicMock()

    # Third execute: aggregate after delete — no reactions remain.
    aggregate_result = MagicMock()
    aggregate_result.all.return_value = []

    db = _mock_db()
    db.execute = AsyncMock(side_effect=[insert_result, delete_result, aggregate_result])

    reactions = await toggle_message_reaction(db, 1, "user-1", "✅")

    # DELETE path: no db.delete(obj), three execute calls (insert, delete, aggregate).
    db.delete.assert_not_called()
    assert db.execute.call_count == 3
    db.commit.assert_called()
    assert reactions == []


@pytest.mark.asyncio
async def test_toggle_twice_adds_then_removes():
    """Toggling the same emoji twice: first call inserts (rowcount=1), second deletes (rowcount=0)."""
    from src.db.store import toggle_message_reaction

    # First toggle: INSERT → rowcount=1 (no conflict, row created).
    insert_result_1 = MagicMock()
    insert_result_1.rowcount = 1
    agg_after_add = MagicMock()
    agg_after_add.all.return_value = [_row(7, "🙌", 1, True)]

    db_add = _mock_db()
    db_add.execute = AsyncMock(side_effect=[insert_result_1, agg_after_add])

    r1 = await toggle_message_reaction(db_add, 7, "user-a", "🙌")
    assert len(r1) == 1
    assert r1[0]["count"] == 1
    db_add.add.assert_not_called()
    assert db_add.execute.call_count == 2

    # Second toggle: INSERT → rowcount=0 (conflict) → DELETE.
    insert_result_2 = MagicMock()
    insert_result_2.rowcount = 0
    delete_result = MagicMock()
    agg_after_del = MagicMock()
    agg_after_del.all.return_value = []

    db_del = _mock_db()
    db_del.execute = AsyncMock(side_effect=[insert_result_2, delete_result, agg_after_del])

    r2 = await toggle_message_reaction(db_del, 7, "user-a", "🙌")
    assert r2 == []
    db_del.delete.assert_not_called()
    assert db_del.execute.call_count == 3


@pytest.mark.asyncio
async def test_toggle_two_users_same_emoji():
    """Two users toggling the same emoji each produce one row."""
    from src.db.store import toggle_message_reaction

    # User A reacts.
    not_found_a = MagicMock()
    not_found_a.scalar_one_or_none.return_value = None
    agg_a = MagicMock()
    agg_a.all.return_value = [_row(3, "👀", 1, True)]

    db_a = _mock_db()
    db_a.execute = AsyncMock(side_effect=[not_found_a, agg_a])
    r_a = await toggle_message_reaction(db_a, 3, "user-a", "👀")
    assert r_a[0]["count"] == 1

    # User B reacts — separate DB "session" shows both reactions.
    not_found_b = MagicMock()
    not_found_b.scalar_one_or_none.return_value = None
    agg_b = MagicMock()
    agg_b.all.return_value = [_row(3, "👀", 2, True)]

    db_b = _mock_db()
    db_b.execute = AsyncMock(side_effect=[not_found_b, agg_b])
    r_b = await toggle_message_reaction(db_b, 3, "user-b", "👀")
    assert r_b[0]["count"] == 2


# ---------------------------------------------------------------------------
# get_session_messages — reactions embedded
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_messages_embeds_reactions():
    """Reactions are embedded in each message that has them."""
    from src.db.store import get_session_messages

    now = time.time()
    msg = MagicMock()
    msg.id = 42
    msg.role = "user"
    msg.content = "hello"
    msg.author_id = "user-1"
    msg.created_at = now
    msg.tool_name = None
    msg.tool_call_id = None
    msg.tool_calls = None
    msg.reply_to_message_id = None
    msg.image_ids = None

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [msg]

    msg_result = MagicMock()
    msg_result.scalars.return_value = scalars_mock

    # Reaction aggregate result.
    reaction_row = _row(42, "👀", 2, True)
    reaction_result = MagicMock()
    reaction_result.all.return_value = [reaction_row]

    db = _mock_db()
    db.execute = AsyncMock(side_effect=[msg_result, reaction_result])

    messages = await get_session_messages(db, "sess-1", user_id="user-1")

    assert len(messages) == 1
    assert "reactions" in messages[0]
    assert messages[0]["reactions"] == [{"emoji": "👀", "count": 2, "reactedByMe": True, "userIds": []}]


@pytest.mark.asyncio
async def test_get_session_messages_no_reactions_absent():
    """Messages with no reactions do not include the 'reactions' key."""
    from src.db.store import get_session_messages

    now = time.time()
    msg = MagicMock()
    msg.id = 10
    msg.role = "user"
    msg.content = "hi"
    msg.author_id = "user-1"
    msg.created_at = now
    msg.tool_name = None
    msg.tool_call_id = None
    msg.tool_calls = None
    msg.reply_to_message_id = None
    msg.image_ids = None

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [msg]

    msg_result = MagicMock()
    msg_result.scalars.return_value = scalars_mock

    # No reactions.
    reaction_result = MagicMock()
    reaction_result.all.return_value = []

    db = _mock_db()
    db.execute = AsyncMock(side_effect=[msg_result, reaction_result])

    messages = await get_session_messages(db, "sess-1", user_id="user-1")

    assert len(messages) == 1
    assert "reactions" not in messages[0]


@pytest.mark.asyncio
async def test_get_session_messages_no_user_id_still_works():
    """get_session_messages works when user_id is not provided (backward compat)."""
    from src.db.store import get_session_messages

    now = time.time()
    msg = MagicMock()
    msg.id = 5
    msg.role = "assistant"
    msg.content = "reply"
    msg.author_id = None
    msg.created_at = now
    msg.tool_name = None
    msg.tool_call_id = None
    msg.tool_calls = None
    msg.reply_to_message_id = None
    msg.image_ids = None

    scalars_mock = MagicMock()
    scalars_mock.all.return_value = [msg]
    msg_result = MagicMock()
    msg_result.scalars.return_value = scalars_mock

    reaction_result = MagicMock()
    reaction_result.all.return_value = []

    db = _mock_db()
    db.execute = AsyncMock(side_effect=[msg_result, reaction_result])

    # Should not raise even without user_id.
    messages = await get_session_messages(db, "sess-1")
    assert len(messages) == 1
    assert messages[0]["content"] == "reply"


# ---------------------------------------------------------------------------
# POST /messages/{message_id}/reactions — endpoint tests
# ---------------------------------------------------------------------------


def _make_app():
    """Minimal FastAPI app wired to the messages router."""
    import types

    for mod_name in ("run_agent", "hermes_state"):
        if mod_name not in sys.modules:
            stub = types.ModuleType(mod_name)
            sys.modules[mod_name] = stub

    for _mod in ("plugins", "plugins.context", "plugins.skills"):
        if _mod not in sys.modules:
            sys.modules[_mod] = types.ModuleType(_mod)

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

    if not hasattr(sys.modules.get("run_agent", MagicMock()), "AIAgent"):
        sys.modules["run_agent"].AIAgent = MagicMock()

    if not hasattr(sys.modules.get("hermes_state", MagicMock()), "SessionDB"):
        class _FakeSessionDB:
            def append_message(self, *a, **kw):
                return 0
            def update_token_counts(self, *a, **kw):
                pass
        sys.modules["hermes_state"].SessionDB = _FakeSessionDB

    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from src.api.routers.messages import router as msg_router

    app = FastAPI()
    app.include_router(msg_router, prefix="/api/v1")

    mock_db = _mock_db()

    @asynccontextmanager
    async def _factory():
        yield mock_db

    app.state.db_session = _factory
    return app, mock_db


@pytest.mark.asyncio
async def test_toggle_reaction_success():
    """POST /messages/{id}/reactions returns 200 with updated reactions list."""
    from httpx import AsyncClient, ASGITransport

    app, mock_db = _make_app()

    # 1st execute: message existence + session_id lookup.
    msg_row = MagicMock()
    msg_row.session_id = "sess-99"
    msg_exists = MagicMock()
    msg_exists.one_or_none.return_value = msg_row

    # 2nd execute: get_session query → session owned by caller (no is_member call).
    session_mock = MagicMock()
    session_mock.user_id = "user-1"
    session_result = MagicMock()
    session_result.scalar_one_or_none.return_value = session_mock

    # 3rd execute: toggle INSERT → rowcount=1 (inserted).
    insert_result = MagicMock()
    insert_result.rowcount = 1

    # 4th execute: aggregate after insert.
    agg = MagicMock()
    agg.all.return_value = [_row(99, "👀", 1, True)]

    mock_db.execute = AsyncMock(side_effect=[msg_exists, session_result, insert_result, agg])

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/messages/99/reactions",
            json={"emoji": "👀"},
            headers={"X-User-Id": "user-1"},
        )

    assert resp.status_code == 200
    data = resp.json()
    assert "reactions" in data
    assert data["reactions"] == [{"emoji": "👀", "count": 1, "reactedByMe": True, "users": []}]


@pytest.mark.asyncio
async def test_toggle_reaction_message_not_found():
    """POST /messages/{id}/reactions returns 404 when message doesn't exist."""
    from httpx import AsyncClient, ASGITransport

    app, mock_db = _make_app()

    msg_missing = MagicMock()
    msg_missing.one_or_none.return_value = None

    mock_db.execute = AsyncMock(return_value=msg_missing)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/messages/9999/reactions",
            json={"emoji": "✅"},
            headers={"X-User-Id": "user-1"},
        )

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_toggle_reaction_empty_emoji_400():
    """POST /messages/{id}/reactions with empty emoji returns 400."""
    from httpx import AsyncClient, ASGITransport

    app, mock_db = _make_app()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/messages/1/reactions",
            json={"emoji": "   "},
            headers={"X-User-Id": "user-1"},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_toggle_reaction_non_numeric_message_id_400():
    """POST /messages/{id}/reactions with non-numeric id returns 400."""
    from httpx import AsyncClient, ASGITransport

    app, mock_db = _make_app()

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/messages/not-a-number/reactions",
            json={"emoji": "👀"},
            headers={"X-User-Id": "user-1"},
        )

    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_toggle_reaction_missing_identity_400():
    """POST /messages/{id}/reactions without X-User-Id returns 400."""
    from httpx import AsyncClient, ASGITransport

    app, mock_db = _make_app()

    # Message exists.
    msg_exists = MagicMock()
    msg_exists.scalar_one_or_none.return_value = 1

    mock_db.execute = AsyncMock(return_value=msg_exists)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.post(
            "/api/v1/messages/1/reactions",
            json={"emoji": "👀"},
            # No X-User-Id header → identity.user_id == ""
        )

    assert resp.status_code == 400
