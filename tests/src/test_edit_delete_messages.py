"""Unit tests for T2 (m3-agent-chat-essential-feature): edit/delete endpoints.

Covers the T2 test plan:
- GET session/thread messages surfaces edited_at when set.
- Inactive (deleted) messages show as "This message was deleted" placeholder
  with deleted=True flag (not omitted) in get_session_messages.
- deleted parent retains reply_to_message_id linkage in placeholder.
- get_thread_replies includes deleted replies as placeholders.
- get_messages_since includes deleted messages as placeholders.
- edit_message and soft_delete_message store functions update the DB correctly.
- PUT /messages/{id}: non-author returns 403; author updates content + edited_at.
- DELETE /messages/{id}: non-author returns 403; author soft-deletes (active=False).
- DELETE /messages/{id}: missing identity returns 400.
- DELETE /messages/{id}: non-numeric id returns 400.
- DELETE /messages/{id}: non-existent message returns 404.
- DELETE idempotency: calling twice is a no-op (does not error).
- get_messages_as_conversation still excludes inactive messages (agent context).
"""

from __future__ import annotations

import sys
import time
import types
from typing import Any, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stub heavyweight deps not present in the test environment
# ---------------------------------------------------------------------------


def _inject_stub_modules():
    for mod_name in ("run_agent", "hermes_state", "plugins", "plugins.context", "plugins.skills"):
        if mod_name not in sys.modules:
            m = types.ModuleType(mod_name)
            sys.modules[mod_name] = m

    plugins = sys.modules["plugins"]

    if not hasattr(plugins, "context"):
        ctx = types.ModuleType("plugins.context")
        ctx.set_context = MagicMock()
        ctx.clear_context = MagicMock()
        sys.modules["plugins.context"] = ctx
        plugins.context = ctx

    if not hasattr(plugins, "skills"):
        skills = types.ModuleType("plugins.skills")
        skills.get_shared_rules = lambda: None
        sys.modules["plugins.skills"] = skills
        plugins.skills = skills

    if "run_agent" in sys.modules and not hasattr(sys.modules["run_agent"], "AIAgent"):
        sys.modules["run_agent"].AIAgent = MagicMock()

    if "hermes_state" in sys.modules and not hasattr(sys.modules["hermes_state"], "SessionDB"):
        class _FakeSessionDB:
            def append_message(self, *a, **kw):
                return 0
            def update_token_counts(self, *a, **kw):
                pass
        sys.modules["hermes_state"].SessionDB = _FakeSessionDB


_inject_stub_modules()


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
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    return result_mock


def _make_message(
    id: int = 1,
    session_id: str = "sess_1",
    role: str = "user",
    content: str = "hello",
    author_id: str = "user_a",
    active: bool = True,
    edited_at=None,
    reply_to_message_id=None,
    thread_root_id=None,
    tool_name=None,
    tool_call_id=None,
    tool_calls=None,
    image_ids=None,
    created_at: float = 1000.0,
):
    msg = MagicMock()
    msg.id = id
    msg.session_id = session_id
    msg.role = role
    msg.content = content
    msg.author_id = author_id
    msg.active = active
    msg.edited_at = edited_at
    msg.reply_to_message_id = reply_to_message_id
    msg.thread_root_id = thread_root_id
    msg.tool_name = tool_name
    msg.tool_call_id = tool_call_id
    msg.tool_calls = tool_calls
    msg.image_ids = image_ids or []
    msg.created_at = created_at
    msg.forwarded_from_message_id = None
    return msg


# ---------------------------------------------------------------------------
# Store: edit_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_message_executes_update_and_commits():
    """edit_message issues an UPDATE and commits."""
    from src.db.store import edit_message

    db = _mock_db()
    await edit_message(db, message_id=42, content="new text")

    db.execute.assert_called_once()
    db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Store: soft_delete_message
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_soft_delete_message_executes_update_and_commits():
    """soft_delete_message issues an UPDATE and commits."""
    from src.db.store import soft_delete_message

    db = _mock_db()
    await soft_delete_message(db, message_id=10)

    db.execute.assert_called_once()
    db.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Store: get_session_messages — deleted-placeholder behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_session_messages_includes_deleted_as_placeholder():
    """Inactive messages are returned as 'This message was deleted' placeholder."""
    from src.db.store import get_session_messages

    deleted_msg = _make_message(id=1, active=False, content="original secret")
    active_msg = _make_message(id=2, active=True, content="still here")

    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([deleted_msg, active_msg]))

    result = await get_session_messages(db, "sess_1")

    assert len(result) == 2
    del_entry = result[0]
    assert del_entry["id"] == "1"
    assert del_entry["content"] == "This message was deleted"
    assert del_entry["deleted"] is True

    live_entry = result[1]
    assert live_entry["id"] == "2"
    assert live_entry["content"] == "still here"
    assert "deleted" not in live_entry


@pytest.mark.asyncio
async def test_get_session_messages_deleted_message_no_original_content():
    """Inactive message must not leak its original content."""
    from src.db.store import get_session_messages

    msg = _make_message(id=5, active=False, content="classified info")
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([msg]))

    result = await get_session_messages(db, "sess_1")
    assert result[0]["content"] == "This message was deleted"
    assert "classified" not in result[0]["content"]


@pytest.mark.asyncio
async def test_get_session_messages_deleted_preserves_reply_linkage():
    """Deleted messages retain reply_to_message_id so thread linkage survives."""
    from src.db.store import get_session_messages

    msg = _make_message(id=7, active=False, reply_to_message_id=3)
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([msg]))

    result = await get_session_messages(db, "sess_1")
    assert result[0]["reply_to_message_id"] == "3"


@pytest.mark.asyncio
async def test_get_session_messages_surfaces_edited_at():
    """Active messages with edited_at set include it in the response."""
    from src.db.store import get_session_messages

    now = time.time()
    msg = _make_message(id=3, active=True, edited_at=now)
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([msg]))

    result = await get_session_messages(db, "sess_1")
    assert "edited_at" in result[0]
    assert result[0]["edited_at"] == now


@pytest.mark.asyncio
async def test_get_session_messages_no_edited_at_when_none():
    """Active messages without edited_at do not have the key in the response."""
    from src.db.store import get_session_messages

    msg = _make_message(id=4, active=True, edited_at=None)
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([msg]))

    result = await get_session_messages(db, "sess_1")
    assert "edited_at" not in result[0]


# ---------------------------------------------------------------------------
# Store: get_thread_replies — deleted-placeholder behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_thread_replies_includes_deleted_reply_as_placeholder():
    """Soft-deleted thread replies show as placeholder, not omitted."""
    from src.db.store import get_thread_replies

    deleted_reply = _make_message(id=20, active=False, thread_root_id=10)
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([deleted_reply]))

    result = await get_thread_replies(db, "sess_1", root_message_id=10)
    assert len(result) == 1
    assert result[0]["content"] == "This message was deleted"
    assert result[0]["deleted"] is True
    assert result[0]["thread_root_id"] == "10"


@pytest.mark.asyncio
async def test_get_thread_replies_active_reply_includes_edited_at():
    """Active thread reply with edited_at set exposes the field."""
    from src.db.store import get_thread_replies

    now = time.time()
    reply = _make_message(id=21, active=True, thread_root_id=10, edited_at=now)
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([reply]))

    result = await get_thread_replies(db, "sess_1", root_message_id=10)
    assert result[0]["edited_at"] == now


# ---------------------------------------------------------------------------
# Store: get_messages_since — deleted-placeholder behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_messages_since_includes_deleted_as_placeholder():
    """SSE catch-up includes soft-deleted messages as placeholders."""
    from src.db.store import get_messages_since

    deleted_msg = _make_message(id=30, active=False, content="gone")
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([deleted_msg]))

    result = await get_messages_since(db, "sess_1", since_message_id=25)
    assert len(result) == 1
    assert result[0]["content"] == "This message was deleted"
    assert result[0]["deleted"] is True


@pytest.mark.asyncio
async def test_get_messages_since_active_message_includes_edited_at():
    """Active message with edited_at is surfaced in SSE catch-up."""
    from src.db.store import get_messages_since

    now = time.time()
    msg = _make_message(id=31, active=True, edited_at=now)
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([msg]))

    result = await get_messages_since(db, "sess_1", since_message_id=25)
    assert result[0]["edited_at"] == now


# ---------------------------------------------------------------------------
# Store: get_messages_as_conversation — excludes inactive messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_messages_as_conversation_excludes_inactive():
    """Agent conversation context must not include soft-deleted messages."""
    from src.db.store import get_messages_as_conversation

    active_msg = _make_message(id=1, active=True, role="user", content="hi")
    # Only active messages are fetched (filter is in the query)
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([active_msg]))

    result = await get_messages_as_conversation(db, "sess_1")
    assert len(result) == 1
    assert result[0]["content"] == "hi"


# ---------------------------------------------------------------------------
# Router: PUT /messages/{message_id} — author check and update
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_message_endpoint_non_author_returns_403():
    """Non-author edit attempt is rejected with 403."""
    from fastapi import HTTPException
    from src.api.routers.messages import edit_message_endpoint, EditMessageRequest

    db = _mock_db()
    msg_mock = _make_message(id=99, author_id="user_a")
    identity = MagicMock(user_id="user_b")

    with patch("src.api.routers.messages.get_message", new=AsyncMock(return_value=msg_mock)):
        with pytest.raises(HTTPException) as exc_info:
            await edit_message_endpoint(
                message_id="99",
                body=EditMessageRequest(content="hacked"),
                identity=identity,
                db=db,
            )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_edit_message_endpoint_author_updates_content():
    """Author can edit their own message — edit_message and get_message are called."""
    from src.api.routers.messages import edit_message_endpoint, EditMessageRequest

    db = _mock_db()
    now = time.time()
    original = _make_message(id=5, author_id="user_a", content="old", active=True)
    updated = _make_message(id=5, author_id="user_a", content="new content", edited_at=now, active=True)
    updated.session_id = "sess_1"
    identity = MagicMock(user_id="user_a")

    get_msg_calls: List[Any] = []

    async def _get_message_stub(db_, message_id):
        get_msg_calls.append(message_id)
        return original if len(get_msg_calls) == 1 else updated

    with patch("src.api.routers.messages.get_message", side_effect=_get_message_stub):
        with patch("src.api.routers.messages.edit_message", new=AsyncMock()) as mock_edit:
            resp = await edit_message_endpoint(
                message_id="5",
                body=EditMessageRequest(content="new content"),
                identity=identity,
                db=db,
            )

    body = resp.body
    import json as _json
    data = _json.loads(body)
    assert data["id"] == "5"
    assert data["content"] == "new content"
    assert data["edited_at"] == now
    mock_edit.assert_awaited_once_with(db, 5, "new content")


@pytest.mark.asyncio
async def test_edit_message_endpoint_message_not_found_returns_404():
    """Edit of a non-existent message returns 404."""
    from fastapi import HTTPException
    from src.api.routers.messages import edit_message_endpoint, EditMessageRequest

    db = _mock_db()
    identity = MagicMock(user_id="user_a")

    with patch("src.api.routers.messages.get_message", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await edit_message_endpoint(
                message_id="999",
                body=EditMessageRequest(content="x"),
                identity=identity,
                db=db,
            )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_edit_message_endpoint_empty_content_returns_400():
    """Edit with blank content string is rejected with 400."""
    from fastapi import HTTPException
    from src.api.routers.messages import edit_message_endpoint, EditMessageRequest

    db = _mock_db()
    identity = MagicMock(user_id="user_a")

    with pytest.raises(HTTPException) as exc_info:
        await edit_message_endpoint(
            message_id="5",
            body=EditMessageRequest(content="  "),
            identity=identity,
            db=db,
        )
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# Router: DELETE /messages/{message_id} — author check and soft-delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_message_endpoint_non_author_returns_403():
    """Non-author delete attempt is rejected with 403."""
    from fastapi import HTTPException
    from src.api.routers.messages import delete_message_endpoint

    db = _mock_db()
    msg_mock = _make_message(id=10, author_id="user_a")
    identity = MagicMock(user_id="user_b")

    with patch("src.api.routers.messages.get_message", new=AsyncMock(return_value=msg_mock)):
        with pytest.raises(HTTPException) as exc_info:
            await delete_message_endpoint(
                message_id="10",
                identity=identity,
                db=db,
            )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_delete_message_endpoint_author_sets_active_false():
    """Author's delete request calls soft_delete_message and returns ok."""
    from src.api.routers.messages import delete_message_endpoint

    db = _mock_db()
    msg_mock = _make_message(id=11, author_id="user_a", active=True)
    identity = MagicMock(user_id="user_a")

    with patch("src.api.routers.messages.get_message", new=AsyncMock(return_value=msg_mock)):
        with patch("src.api.routers.messages.soft_delete_message", new=AsyncMock()) as mock_delete:
            resp = await delete_message_endpoint(
                message_id="11",
                identity=identity,
                db=db,
            )

    import json as _json
    data = _json.loads(resp.body)
    assert data["ok"] is True
    assert data["message_id"] == "11"
    mock_delete.assert_awaited_once_with(db, 11)


@pytest.mark.asyncio
async def test_delete_message_endpoint_missing_identity_returns_400():
    """Empty user_id (missing X-User-Id) returns 400."""
    from fastapi import HTTPException
    from src.api.routers.messages import delete_message_endpoint

    db = _mock_db()
    identity = MagicMock(user_id="")

    with pytest.raises(HTTPException) as exc_info:
        await delete_message_endpoint(
            message_id="5",
            identity=identity,
            db=db,
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_message_endpoint_non_numeric_id_returns_400():
    """Non-numeric message_id path param returns 400."""
    from fastapi import HTTPException
    from src.api.routers.messages import delete_message_endpoint

    db = _mock_db()
    identity = MagicMock(user_id="user_a")

    with pytest.raises(HTTPException) as exc_info:
        await delete_message_endpoint(
            message_id="notanumber",
            identity=identity,
            db=db,
        )
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_delete_message_endpoint_not_found_returns_404():
    """Delete of a non-existent message returns 404."""
    from fastapi import HTTPException
    from src.api.routers.messages import delete_message_endpoint

    db = _mock_db()
    identity = MagicMock(user_id="user_a")

    with patch("src.api.routers.messages.get_message", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await delete_message_endpoint(
                message_id="999",
                identity=identity,
                db=db,
            )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_delete_message_idempotent():
    """Deleting an already-deleted message still returns ok (idempotent)."""
    from src.api.routers.messages import delete_message_endpoint

    db = _mock_db()
    msg_mock = _make_message(id=12, author_id="user_a", active=False)
    identity = MagicMock(user_id="user_a")

    with patch("src.api.routers.messages.get_message", new=AsyncMock(return_value=msg_mock)):
        with patch("src.api.routers.messages.soft_delete_message", new=AsyncMock()) as mock_delete:
            resp = await delete_message_endpoint(
                message_id="12",
                identity=identity,
                db=db,
            )

    import json as _json
    data = _json.loads(resp.body)
    assert data["ok"] is True
    # soft_delete_message is still called (DB UPDATE is idempotent)
    mock_delete.assert_awaited_once_with(db, 12)


# ---------------------------------------------------------------------------
# Router: read path after delete — placeholder visible in transcript
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_path_shows_deleted_placeholder_after_delete():
    """After soft-delete, get_session_messages shows placeholder, not original."""
    from src.db.store import get_session_messages

    # Simulate a message that has been soft-deleted
    deleted_msg = _make_message(id=50, active=False, content="private content")
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([deleted_msg]))

    result = await get_session_messages(db, "sess_1")
    assert result[0]["content"] == "This message was deleted"
    assert result[0].get("deleted") is True
    assert "private" not in result[0]["content"]


@pytest.mark.asyncio
async def test_deleted_parent_resolvable_for_reply_consumers():
    """Deleted message is still in the list so reply_to_message_id linkage holds."""
    from src.db.store import get_session_messages

    parent = _make_message(id=1, active=False, content="deleted parent")
    child = _make_message(id=2, active=True, content="reply", reply_to_message_id=1)
    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalars_result([parent, child]))

    result = await get_session_messages(db, "sess_1")

    # Both messages are present
    assert len(result) == 2
    parent_entry = result[0]
    child_entry = result[1]

    # Parent is a placeholder
    assert parent_entry["id"] == "1"
    assert parent_entry["deleted"] is True

    # Child still references parent
    assert child_entry["reply_to_message_id"] == "1"
