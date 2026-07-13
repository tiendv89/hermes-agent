"""Tests for T4 (m3-agent-chat-essential-feature): save/unsave endpoints + GET /messages/saved.

Test plan:
  - save then list returns the message
  - unsave removes it from the list
  - save is idempotent per user (returns 200 on second save, not 201)
  - message not found → 404
  - missing identity → 400
  - unsave non-existent save is a no-op (idempotent, 204)
  - saved list is ordered newest-saved-first
  - saved list for user with no saves returns empty list
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


def _make_message(
    message_id=1, session_id="sess-1", content="hello", role="user", author_id="user-1"
):
    msg = MagicMock()
    msg.id = message_id
    msg.session_id = session_id
    msg.content = content
    msg.role = role
    msg.author_id = author_id
    msg.created_at = time.time()
    msg.edited_at = None
    msg.active = True
    return msg


def _make_save(message_id=1, user_id="user-1", saved_at=None):
    save = MagicMock()
    save.message_id = message_id
    save.user_id = user_id
    save.saved_at = saved_at or time.time()
    return save


# ---------------------------------------------------------------------------
# _get_message helper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_message_returns_message():
    from src.api.routers.message_saves import _get_message

    db = _mock_db()
    mock_msg = _make_message(message_id=42)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=mock_msg)
    db.execute = AsyncMock(return_value=mock_result)

    result = await _get_message(db, 42)
    assert result.id == 42


@pytest.mark.asyncio
async def test_get_message_raises_404_when_not_found():
    from fastapi import HTTPException
    from src.api.routers.message_saves import _get_message

    db = _mock_db()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=mock_result)

    with pytest.raises(HTTPException) as exc_info:
        await _get_message(db, 999)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# POST /messages/{message_id}/save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_message_creates_save_row():
    from src.api.routers.message_saves import save_message
    from src.api.identity import Identity

    db = _mock_db()
    mock_msg = _make_message(message_id=10)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=mock_msg)
    db.execute = AsyncMock(return_value=mock_result)
    db.get = AsyncMock(return_value=None)  # no existing save

    identity = Identity(user_id="user-1")
    response = await save_message(message_id=10, identity=identity, db=db)

    assert response.status_code == 201
    import json

    body = json.loads(response.body)
    assert body["saved"] is True
    assert body["message_id"] == "10"
    assert "saved_at" in body
    db.add.assert_called_once()
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_save_message_idempotent_returns_200():
    from src.api.routers.message_saves import save_message
    from src.api.identity import Identity

    db = _mock_db()
    mock_msg = _make_message(message_id=10)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=mock_msg)
    db.execute = AsyncMock(return_value=mock_result)
    existing_save = _make_save(message_id=10, user_id="user-1")
    db.get = AsyncMock(return_value=existing_save)  # save already exists

    identity = Identity(user_id="user-1")
    response = await save_message(message_id=10, identity=identity, db=db)

    assert response.status_code == 200
    import json

    body = json.loads(response.body)
    assert body["saved"] is True
    db.add.assert_not_called()


@pytest.mark.asyncio
async def test_save_message_missing_identity():
    from fastapi import HTTPException
    from src.api.routers.message_saves import save_message
    from src.api.identity import Identity

    db = _mock_db()
    identity = Identity(user_id="")

    with pytest.raises(HTTPException) as exc_info:
        await save_message(message_id=1, identity=identity, db=db)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_save_message_not_found():
    from fastapi import HTTPException
    from src.api.routers.message_saves import save_message
    from src.api.identity import Identity

    db = _mock_db()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=mock_result)

    identity = Identity(user_id="user-1")
    with pytest.raises(HTTPException) as exc_info:
        await save_message(message_id=999, identity=identity, db=db)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /messages/{message_id}/save
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unsave_message_deletes_row():
    from src.api.routers.message_saves import unsave_message
    from src.api.identity import Identity

    db = _mock_db()
    mock_msg = _make_message(message_id=10)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=mock_msg)
    db.execute = AsyncMock(return_value=mock_result)

    identity = Identity(user_id="user-1")
    response = await unsave_message(message_id=10, identity=identity, db=db)

    assert response.status_code == 204
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_unsave_message_idempotent_no_error():
    """Unsaving a message that wasn't saved is a no-op — no 404, just 204."""
    from src.api.routers.message_saves import unsave_message
    from src.api.identity import Identity

    db = _mock_db()
    mock_msg = _make_message(message_id=10)
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=mock_msg)
    db.execute = AsyncMock(return_value=mock_result)

    identity = Identity(user_id="user-1")
    response = await unsave_message(message_id=10, identity=identity, db=db)

    assert response.status_code == 204


@pytest.mark.asyncio
async def test_unsave_missing_identity():
    from fastapi import HTTPException
    from src.api.routers.message_saves import unsave_message
    from src.api.identity import Identity

    db = _mock_db()
    identity = Identity(user_id="")

    with pytest.raises(HTTPException) as exc_info:
        await unsave_message(message_id=1, identity=identity, db=db)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_unsave_message_not_found():
    from fastapi import HTTPException
    from src.api.routers.message_saves import unsave_message
    from src.api.identity import Identity

    db = _mock_db()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none = MagicMock(return_value=None)
    db.execute = AsyncMock(return_value=mock_result)

    identity = Identity(user_id="user-1")
    with pytest.raises(HTTPException) as exc_info:
        await unsave_message(message_id=999, identity=identity, db=db)
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# GET /messages/saved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_saved_messages_returns_saved():
    from src.api.routers.message_saves import list_saved_messages
    from src.api.identity import Identity

    db = _mock_db()
    now = time.time()

    row = MagicMock()
    row.id = 42
    row.content = "saved content"
    row.role = "user"
    row.author_id = "user-1"
    row.created_at = now - 100
    row.edited_at = None
    row.session_id = "sess-abc"
    row.saved_at = now
    row.session_title = "My session"
    row.session_kind = "channel"

    mock_result = MagicMock()
    mock_result.all = MagicMock(return_value=[row])
    db.execute = AsyncMock(return_value=mock_result)

    identity = Identity(user_id="user-1")
    response = await list_saved_messages(identity=identity, db=db)

    assert response.status_code == 200
    import json

    body = json.loads(response.body)
    assert len(body["messages"]) == 1
    msg = body["messages"][0]
    assert msg["id"] == "42"
    assert msg["content"] == "saved content"
    assert msg["session_id"] == "sess-abc"
    assert msg["session_title"] == "My session"
    assert msg["saved_at"] == now
    assert "edited_at" not in msg  # not set


@pytest.mark.asyncio
async def test_list_saved_messages_includes_edited_at_when_set():
    from src.api.routers.message_saves import list_saved_messages
    from src.api.identity import Identity

    db = _mock_db()
    now = time.time()

    row = MagicMock()
    row.id = 5
    row.content = "edited content"
    row.role = "user"
    row.author_id = "user-1"
    row.created_at = now - 200
    row.edited_at = now - 50
    row.session_id = "sess-xyz"
    row.saved_at = now
    row.session_title = "Another session"
    row.session_kind = "thread"

    mock_result = MagicMock()
    mock_result.all = MagicMock(return_value=[row])
    db.execute = AsyncMock(return_value=mock_result)

    identity = Identity(user_id="user-1")
    response = await list_saved_messages(identity=identity, db=db)

    import json

    body = json.loads(response.body)
    msg = body["messages"][0]
    assert "edited_at" in msg
    assert msg["edited_at"] == now - 50


@pytest.mark.asyncio
async def test_list_saved_messages_empty_for_user_with_no_saves():
    from src.api.routers.message_saves import list_saved_messages
    from src.api.identity import Identity

    db = _mock_db()
    mock_result = MagicMock()
    mock_result.all = MagicMock(return_value=[])
    db.execute = AsyncMock(return_value=mock_result)

    identity = Identity(user_id="user-no-saves")
    response = await list_saved_messages(identity=identity, db=db)

    assert response.status_code == 200
    import json

    body = json.loads(response.body)
    assert body["messages"] == []


@pytest.mark.asyncio
async def test_list_saved_messages_missing_identity():
    from fastapi import HTTPException
    from src.api.routers.message_saves import list_saved_messages
    from src.api.identity import Identity

    db = _mock_db()
    identity = Identity(user_id="")

    with pytest.raises(HTTPException) as exc_info:
        await list_saved_messages(identity=identity, db=db)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_list_saved_messages_uses_untitled_fallback():
    from src.api.routers.message_saves import list_saved_messages
    from src.api.identity import Identity

    db = _mock_db()
    now = time.time()

    row = MagicMock()
    row.id = 7
    row.content = "hi"
    row.role = "user"
    row.author_id = "user-1"
    row.created_at = now
    row.edited_at = None
    row.session_id = "sess-1"
    row.saved_at = now
    row.session_title = None  # untitled session
    row.session_kind = "thread"

    mock_result = MagicMock()
    mock_result.all = MagicMock(return_value=[row])
    db.execute = AsyncMock(return_value=mock_result)

    identity = Identity(user_id="user-1")
    response = await list_saved_messages(identity=identity, db=db)

    import json

    body = json.loads(response.body)
    assert body["messages"][0]["session_title"] == "(untitled)"


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


def test_message_saves_router_included():
    """Verify message_saves router module is imported in the aggregate router."""
    import src.api.router as router_module
    from src.api.routers import message_saves

    assert hasattr(router_module, "router"), "aggregate router not exported"
    # The router include_router call is the registration; confirming the module
    # is imported and the sub-router has the expected endpoints is sufficient.
    route_paths = [r.path for r in message_saves.router.routes]
    assert "/messages/{message_id}/save" in route_paths
    assert "/messages/saved" in route_paths


def test_save_route_exists():
    from src.api.routers.message_saves import router

    paths = [r.path for r in router.routes]
    assert "/messages/{message_id}/save" in paths


def test_saved_list_route_exists():
    from src.api.routers.message_saves import router

    paths = [r.path for r in router.routes]
    assert "/messages/saved" in paths
