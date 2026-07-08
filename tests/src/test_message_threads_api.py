"""Integration tests for T3: message_threads router + SendMessageRequest.reply_to_message_id.

Covers:
- POST /threads/{session_id}/messages/{message_id}/replies:
    - Successful reply (stores thread_root_id + reply_to_message_id)
    - Nested-thread rejection: 400 nested_thread_not_supported
    - Non-existent root message: 404
    - Non-existent session: 404
    - Non-member caller: 403
    - Empty content: 400
    - Non-numeric message_id: 400
    - SSE payload includes thread_root_id and reply_to_message_id
    - Agent dispatch gate: @agent mention inside thread triggers dispatch
    - Agent dispatch gate: bare message in channel (kind='channel') does NOT trigger
- GET /threads/{session_id}/messages/{message_id}/replies:
    - Returns replies oldest-first; empty list when no replies
    - Respects ?since= cursor
    - Returns 404 for non-existent session
- GET /threads/{session_id}/messages (get_thread_messages):
    - Includes thread_summary on messages that have replies
    - Omits thread_summary on messages with no replies
- SendMessageRequest.reply_to_message_id:
    - Passes through to append_message as reply_to_message_id
    - Invalid (non-numeric) value returns 400
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Stubs for heavyweight deps not present in test env
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
    return db


def _scalars_result(rows):
    scalars_mock = MagicMock()
    scalars_mock.all.return_value = rows
    result_mock = MagicMock()
    result_mock.scalars.return_value = scalars_mock
    return result_mock


def _rows_result(rows):
    result_mock = MagicMock()
    result_mock.all.return_value = rows
    return result_mock


def _mock_session(session_id="sess-1", kind="thread", workspace_id="ws-1", user_id="owner-1"):
    s = MagicMock()
    s.id = session_id
    s.kind = kind
    s.workspace_id = workspace_id
    s.user_id = user_id
    s.feature_id = ""
    s.model = None
    s.title = "Test"
    return s


def _mock_message(
    id=10,
    session_id="sess-1",
    thread_root_id=None,
    role="user",
    content="root msg",
    author_id="u1",
):
    m = MagicMock()
    m.id = id
    m.session_id = session_id
    m.thread_root_id = thread_root_id
    m.role = role
    m.content = content
    m.author_id = author_id
    m.created_at = 1000.0
    m.reply_to_message_id = None
    m.tool_name = None
    m.tool_call_id = None
    m.tool_calls = None
    m.active = True
    return m


# ---------------------------------------------------------------------------
# POST /threads/{session_id}/messages/{message_id}/replies — success path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_thread_reply_success():
    """Successful reply stores correct thread_root_id and reply_to_message_id."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest

    db = _mock_db()
    root_msg = _mock_message(id=10, thread_root_id=None)
    db.get = AsyncMock(return_value=root_msg)

    captured_kwargs: Dict[str, Any] = {}

    async def _fake_append(db_, session_id, role, content, author_id=None,
                           thread_root_id=None, reply_to_message_id=None, **kw):
        captured_kwargs.update({
            "thread_root_id": thread_root_id,
            "reply_to_message_id": reply_to_message_id,
        })
        return 999

    session = _mock_session()
    published: list = []

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.is_member", AsyncMock(return_value=True)), \
         patch("src.api.routers.message_threads.get_workspace_organization_id", AsyncMock(return_value="")), \
         patch("src.api.routers.message_threads.mention_candidates", AsyncMock(return_value=[])), \
         patch("src.api.routers.message_threads.resolve_mentions", return_value=[]), \
         patch("src.api.routers.message_threads.append_message", _fake_append), \
         patch("src.api.routers.message_threads.persist_mentions", AsyncMock()), \
         patch("src.api.routers.message_threads.touch_session", AsyncMock()), \
         patch("src.api.routers.message_threads.author_for", AsyncMock(return_value=None)), \
         patch("src.api.routers.message_threads.get_bus") as mock_bus, \
         patch("src.api.routers.message_threads._should_trigger_agent", return_value=False):

        mock_bus.return_value.publish = lambda sid, payload: published.append(payload)

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = "org1"

        body = PostThreadReplyRequest(content="hello thread")
        response = await post_thread_reply(
            session_id="sess-1",
            message_id="10",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert response.status_code == 202
    assert captured_kwargs["thread_root_id"] == 10
    # Default reply_to_message_id is the root (10)
    assert captured_kwargs["reply_to_message_id"] == 10


@pytest.mark.asyncio
async def test_post_thread_reply_explicit_inner_reply_to():
    """reply_to_message_id in body sets a specific inner reply target."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest

    db = _mock_db()
    root_msg = _mock_message(id=10, thread_root_id=None)
    db.get = AsyncMock(return_value=root_msg)

    captured_kwargs: Dict[str, Any] = {}

    async def _fake_append(db_, session_id, role, content, author_id=None,
                           thread_root_id=None, reply_to_message_id=None, **kw):
        captured_kwargs.update({
            "thread_root_id": thread_root_id,
            "reply_to_message_id": reply_to_message_id,
        })
        return 1000

    session = _mock_session()

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.is_member", AsyncMock(return_value=True)), \
         patch("src.api.routers.message_threads.get_workspace_organization_id", AsyncMock(return_value="")), \
         patch("src.api.routers.message_threads.mention_candidates", AsyncMock(return_value=[])), \
         patch("src.api.routers.message_threads.resolve_mentions", return_value=[]), \
         patch("src.api.routers.message_threads.append_message", _fake_append), \
         patch("src.api.routers.message_threads.persist_mentions", AsyncMock()), \
         patch("src.api.routers.message_threads.touch_session", AsyncMock()), \
         patch("src.api.routers.message_threads.author_for", AsyncMock(return_value=None)), \
         patch("src.api.routers.message_threads.get_bus") as mock_bus, \
         patch("src.api.routers.message_threads._should_trigger_agent", return_value=False):

        mock_bus.return_value.publish = MagicMock()

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="reply to reply", reply_to_message_id="55")
        await post_thread_reply(
            session_id="sess-1",
            message_id="10",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert captured_kwargs["thread_root_id"] == 10
    assert captured_kwargs["reply_to_message_id"] == 55


@pytest.mark.asyncio
async def test_post_thread_reply_sse_payload_includes_thread_fields():
    """SSE message.created payload includes thread_root_id and reply_to_message_id."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest

    db = _mock_db()
    root_msg = _mock_message(id=7, thread_root_id=None)
    db.get = AsyncMock(return_value=root_msg)

    published: list = []

    async def _fake_append(db_, session_id, role, content, author_id=None,
                           thread_root_id=None, reply_to_message_id=None, **kw):
        return 201

    session = _mock_session()

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.is_member", AsyncMock(return_value=True)), \
         patch("src.api.routers.message_threads.get_workspace_organization_id", AsyncMock(return_value="")), \
         patch("src.api.routers.message_threads.mention_candidates", AsyncMock(return_value=[])), \
         patch("src.api.routers.message_threads.resolve_mentions", return_value=[]), \
         patch("src.api.routers.message_threads.append_message", _fake_append), \
         patch("src.api.routers.message_threads.persist_mentions", AsyncMock()), \
         patch("src.api.routers.message_threads.touch_session", AsyncMock()), \
         patch("src.api.routers.message_threads.author_for", AsyncMock(return_value=None)), \
         patch("src.api.routers.message_threads.get_bus") as mock_bus, \
         patch("src.api.routers.message_threads._should_trigger_agent", return_value=False):

        mock_bus.return_value.publish = lambda sid, payload: published.append(payload)

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u2"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="my reply")
        await post_thread_reply(
            session_id="sess-1",
            message_id="7",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert len(published) == 1
    data = published[0]["data"]
    assert data["thread_root_id"] == "7"
    assert data["reply_to_message_id"] == "7"  # default to root


# ---------------------------------------------------------------------------
# POST — rejection cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_thread_reply_nested_thread_rejected():
    """Returns 400 nested_thread_not_supported when root msg has thread_root_id != NULL."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest
    from fastapi import HTTPException

    db = _mock_db()
    # The target message is itself a reply (thread_root_id is set) — cannot nest further.
    nested_msg = _mock_message(id=20, thread_root_id=5)
    db.get = AsyncMock(return_value=nested_msg)

    session = _mock_session()

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.is_member", AsyncMock(return_value=True)):

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="nested reply")
        with pytest.raises(HTTPException) as exc_info:
            await post_thread_reply(
                session_id="sess-1",
                message_id="20",
                body=body,
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 400
    assert "nested_thread_not_supported" in exc_info.value.detail


@pytest.mark.asyncio
async def test_post_thread_reply_root_message_not_found():
    """Returns 404 when the root message does not exist."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest
    from fastapi import HTTPException

    db = _mock_db()
    db.get = AsyncMock(return_value=None)  # message not found

    session = _mock_session()

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.is_member", AsyncMock(return_value=True)):

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="reply")
        with pytest.raises(HTTPException) as exc_info:
            await post_thread_reply(
                session_id="sess-1",
                message_id="999",
                body=body,
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_post_thread_reply_session_not_found():
    """Returns 404 when the session does not exist."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest
    from fastapi import HTTPException

    db = _mock_db()

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=None)):
        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="reply")
        with pytest.raises(HTTPException) as exc_info:
            await post_thread_reply(
                session_id="nonexistent",
                message_id="10",
                body=body,
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_post_thread_reply_non_member_forbidden():
    """Returns 403 when the caller is not a session member."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest
    from fastapi import HTTPException

    db = _mock_db()
    session = _mock_session(user_id="owner-99")  # different owner

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.is_member", AsyncMock(return_value=False)):

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "outsider"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="reply")
        with pytest.raises(HTTPException) as exc_info:
            await post_thread_reply(
                session_id="sess-1",
                message_id="10",
                body=body,
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_post_thread_reply_empty_content_rejected():
    """Returns 400 for empty content."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest
    from fastapi import HTTPException

    db = _mock_db()
    session = _mock_session()

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)):
        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="  ")
        with pytest.raises(HTTPException) as exc_info:
            await post_thread_reply(
                session_id="sess-1",
                message_id="10",
                body=body,
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_post_thread_reply_non_numeric_message_id():
    """Returns 400 for a non-numeric message_id path parameter."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest
    from fastapi import HTTPException

    db = _mock_db()
    session = _mock_session()

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.is_member", AsyncMock(return_value=True)):

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="reply")
        with pytest.raises(HTTPException) as exc_info:
            await post_thread_reply(
                session_id="sess-1",
                message_id="not-a-number",
                body=body,
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_post_thread_reply_root_message_wrong_session():
    """Returns 404 when the message belongs to a different session."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest
    from fastapi import HTTPException

    db = _mock_db()
    wrong_session_msg = _mock_message(id=10, session_id="sess-other", thread_root_id=None)
    db.get = AsyncMock(return_value=wrong_session_msg)
    session = _mock_session(session_id="sess-1")

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.is_member", AsyncMock(return_value=True)):

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="reply")
        with pytest.raises(HTTPException) as exc_info:
            await post_thread_reply(
                session_id="sess-1",
                message_id="10",
                body=body,
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# POST — agent dispatch gate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_thread_reply_agent_mention_triggers_dispatch():
    """@agent mention inside a thread reply triggers schedule_agent_turn."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest

    db = _mock_db()
    root_msg = _mock_message(id=10, thread_root_id=None)
    db.get = AsyncMock(return_value=root_msg)

    schedule_called = []
    session = _mock_session(kind="thread")

    async def _fake_append(db_, session_id, role, content, author_id=None,
                           thread_root_id=None, reply_to_message_id=None, **kw):
        return 500

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.is_member", AsyncMock(return_value=True)), \
         patch("src.api.routers.message_threads.get_workspace_organization_id", AsyncMock(return_value="")), \
         patch("src.api.routers.message_threads.mention_candidates", AsyncMock(return_value=[])), \
         patch("src.api.routers.message_threads.resolve_mentions", return_value=[{"mentioned_id": "agent", "mentioned_kind": "agent"}]), \
         patch("src.api.routers.message_threads.append_message", _fake_append), \
         patch("src.api.routers.message_threads.persist_mentions", AsyncMock()), \
         patch("src.api.routers.message_threads.touch_session", AsyncMock()), \
         patch("src.api.routers.message_threads.author_for", AsyncMock(return_value=None)), \
         patch("src.api.routers.message_threads.get_bus") as mock_bus, \
         patch("src.api.routers.message_threads.get_messages_as_conversation", AsyncMock(return_value=[])), \
         patch("src.api.routers.message_threads.default_model", AsyncMock(return_value="gpt-4")), \
         patch("src.api.routers.message_threads.resolve_model", AsyncMock(return_value={"model": "gpt-4", "provider": None, "api_key": None, "base_url": None})), \
         patch("src.api.routers.message_threads.update_session_model", AsyncMock()), \
         patch("src.api.routers.message_threads.schedule_agent_turn", AsyncMock(side_effect=lambda **kw: schedule_called.append(kw))):

        mock_bus.return_value.publish = MagicMock()

        request = MagicMock()
        request.app.state.db_session = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="@agent help me")
        response = await post_thread_reply(
            session_id="sess-1",
            message_id="10",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert response.status_code == 202
    assert len(schedule_called) == 1


@pytest.mark.asyncio
async def test_post_thread_reply_channel_bare_message_no_dispatch():
    """Bare message in a channel session does NOT trigger agent dispatch."""
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest

    db = _mock_db()
    root_msg = _mock_message(id=10, thread_root_id=None)
    db.get = AsyncMock(return_value=root_msg)

    schedule_called = []
    session = _mock_session(kind="channel")

    async def _fake_append(db_, session_id, role, content, author_id=None,
                           thread_root_id=None, reply_to_message_id=None, **kw):
        return 501

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.is_member", AsyncMock(return_value=True)), \
         patch("src.api.routers.message_threads.get_workspace_organization_id", AsyncMock(return_value="")), \
         patch("src.api.routers.message_threads.mention_candidates", AsyncMock(return_value=[])), \
         patch("src.api.routers.message_threads.resolve_mentions", return_value=[]), \
         patch("src.api.routers.message_threads.append_message", _fake_append), \
         patch("src.api.routers.message_threads.persist_mentions", AsyncMock()), \
         patch("src.api.routers.message_threads.touch_session", AsyncMock()), \
         patch("src.api.routers.message_threads.author_for", AsyncMock(return_value=None)), \
         patch("src.api.routers.message_threads.get_bus") as mock_bus, \
         patch("src.api.routers.message_threads.schedule_agent_turn", AsyncMock(side_effect=lambda **kw: schedule_called.append(kw))):

        mock_bus.return_value.publish = MagicMock()

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = PostThreadReplyRequest(content="just a message")
        response = await post_thread_reply(
            session_id="sess-1",
            message_id="10",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert response.status_code == 202
    assert len(schedule_called) == 0
    import json
    data = json.loads(response.body)
    assert data["agent_triggered"] is False


# ---------------------------------------------------------------------------
# GET /threads/{session_id}/messages/{message_id}/replies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_thread_replies_returns_replies_oldest_first():
    """Returns replies oldest-first, author-enriched."""
    from src.api.routers.message_threads import get_message_thread_replies

    db = _mock_db()
    replies = [
        {"id": "101", "session_id": "sess-1", "role": "user", "content": "r1",
         "author_id": "u1", "created_at": 1.0, "thread_root_id": "10"},
        {"id": "102", "session_id": "sess-1", "role": "user", "content": "r2",
         "author_id": "u2", "created_at": 2.0, "thread_root_id": "10"},
    ]
    session = _mock_session()

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.get_thread_replies", AsyncMock(return_value=replies)), \
         patch("src.api.routers.message_threads.attach_authors", AsyncMock(return_value=replies)):

        response = await get_message_thread_replies(
            session_id="sess-1",
            message_id="10",
            since="",
            _identity=MagicMock(),
            db=db,
        )

    import json
    data = json.loads(response.body)
    assert len(data["replies"]) == 2
    assert data["replies"][0]["id"] == "101"
    assert data["replies"][1]["id"] == "102"


@pytest.mark.asyncio
async def test_get_thread_replies_empty():
    """Returns empty list when thread has no replies."""
    from src.api.routers.message_threads import get_message_thread_replies

    db = _mock_db()
    session = _mock_session()

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.get_thread_replies", AsyncMock(return_value=[])), \
         patch("src.api.routers.message_threads.attach_authors", AsyncMock(return_value=[])):

        response = await get_message_thread_replies(
            session_id="sess-1",
            message_id="10",
            since="",
            _identity=MagicMock(),
            db=db,
        )

    import json
    data = json.loads(response.body)
    assert data["replies"] == []


@pytest.mark.asyncio
async def test_get_thread_replies_passes_since_cursor():
    """Passes the ?since= cursor to get_thread_replies."""
    from src.api.routers.message_threads import get_message_thread_replies

    db = _mock_db()
    session = _mock_session()
    captured: list = []

    async def _fake_get_replies(db_, session_id, root_id, since=None):
        captured.append(since)
        return []

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.message_threads.get_thread_replies", _fake_get_replies), \
         patch("src.api.routers.message_threads.attach_authors", AsyncMock(return_value=[])):

        await get_message_thread_replies(
            session_id="sess-1",
            message_id="10",
            since="50",
            _identity=MagicMock(),
            db=db,
        )

    assert captured == [50]


@pytest.mark.asyncio
async def test_get_thread_replies_session_not_found():
    """Returns 404 when the session does not exist."""
    from src.api.routers.message_threads import get_message_thread_replies
    from fastapi import HTTPException

    db = _mock_db()

    with patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc_info:
            await get_message_thread_replies(
                session_id="nonexistent",
                message_id="10",
                since="",
                _identity=MagicMock(),
                db=db,
            )

    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# GET /threads/{session_id}/messages — thread_summary attachment
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_thread_messages_includes_thread_summary():
    """get_thread_messages attaches thread_summary to messages that have replies."""
    from src.api.routers.messages import get_thread_messages

    db = _mock_db()
    # get_session_messages returns a top-level message
    msgs = [
        {"id": "10", "role": "user", "content": "root", "author_id": "u1", "created_at": 1.0},
        {"id": "11", "role": "user", "content": "other", "author_id": "u2", "created_at": 2.0},
    ]
    session = _mock_session()
    summary = {10: {"reply_count": 3, "recent_repliers": ["u2", "u3"]}}

    with patch("src.api.routers.messages.get_session_messages", AsyncMock(return_value=msgs)), \
         patch("src.api.routers.messages.get_messages_since", AsyncMock(return_value=[])), \
         patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.messages.attach_authors", AsyncMock(return_value=msgs)), \
         patch("src.api.routers.messages.get_thread_reply_summaries", AsyncMock(return_value=summary)):

        response = await get_thread_messages(
            session_id="sess-1",
            since="",
            _identity=MagicMock(),
            db=db,
        )

    import json
    data = json.loads(response.body)
    messages = data["messages"]
    msg_10 = next(m for m in messages if m["id"] == "10")
    msg_11 = next(m for m in messages if m["id"] == "11")

    assert "thread_summary" in msg_10
    assert msg_10["thread_summary"]["reply_count"] == 3
    assert msg_10["thread_summary"]["recent_repliers"] == ["u2", "u3"]
    # msg 11 has no replies — no thread_summary key
    assert "thread_summary" not in msg_11


@pytest.mark.asyncio
async def test_get_thread_messages_no_thread_summary_when_no_replies():
    """Messages with no replies have no thread_summary key."""
    from src.api.routers.messages import get_thread_messages

    db = _mock_db()
    msgs = [
        {"id": "20", "role": "user", "content": "msg", "author_id": "u1", "created_at": 1.0},
    ]
    session = _mock_session()

    with patch("src.api.routers.messages.get_session_messages", AsyncMock(return_value=msgs)), \
         patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.messages.attach_authors", AsyncMock(return_value=msgs)), \
         patch("src.api.routers.messages.get_thread_reply_summaries", AsyncMock(return_value={})):

        response = await get_thread_messages(
            session_id="sess-1",
            since="",
            _identity=MagicMock(),
            db=db,
        )

    import json
    data = json.loads(response.body)
    assert "thread_summary" not in data["messages"][0]


# ---------------------------------------------------------------------------
# SendMessageRequest.reply_to_message_id (inline G1 reply)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_passes_reply_to_message_id():
    """send_message passes reply_to_message_id to append_message."""
    from src.api.routers.messages import send_message, SendMessageRequest

    db = _mock_db()
    captured: Dict[str, Any] = {}

    async def _fake_append(db_, session_id, role, content, author_id=None,
                           reply_to_message_id=None, thread_root_id=None, **kw):
        captured["reply_to_message_id"] = reply_to_message_id
        return 300

    session = _mock_session()

    with patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.messages.is_member", AsyncMock(return_value=True)), \
         patch("src.api.routers.messages.get_workspace_organization_id", AsyncMock(return_value="")), \
         patch("src.api.routers.messages.mention_candidates", AsyncMock(return_value=[])), \
         patch("src.api.routers.messages.resolve_mentions", return_value=[]), \
         patch("src.api.routers.messages.append_message", _fake_append), \
         patch("src.api.routers.messages.persist_mentions", AsyncMock()), \
         patch("src.api.routers.messages.touch_session", AsyncMock()), \
         patch("src.api.routers.messages.set_session_title", AsyncMock()), \
         patch("src.api.routers.messages.author_for", AsyncMock(return_value=None)), \
         patch("src.api.routers.messages.get_bus") as mock_bus, \
         patch("src.api.routers.messages._should_trigger_agent", return_value=False):

        mock_bus.return_value.publish = MagicMock()

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = SendMessageRequest(content="my inline reply", reply_to_message_id="42")
        await send_message(
            session_id="sess-1",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert captured["reply_to_message_id"] == 42


@pytest.mark.asyncio
async def test_send_message_invalid_reply_to_message_id():
    """send_message returns 400 for a non-numeric reply_to_message_id."""
    from src.api.routers.messages import send_message, SendMessageRequest
    from fastapi import HTTPException

    db = _mock_db()
    session = _mock_session()

    with patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.messages.is_member", AsyncMock(return_value=True)), \
         patch("src.api.routers.messages.get_workspace_organization_id", AsyncMock(return_value="")), \
         patch("src.api.routers.messages.mention_candidates", AsyncMock(return_value=[])), \
         patch("src.api.routers.messages.resolve_mentions", return_value=[]):

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = SendMessageRequest(content="reply", reply_to_message_id="not-a-number")
        with pytest.raises(HTTPException) as exc_info:
            await send_message(
                session_id="sess-1",
                body=body,
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_send_message_without_reply_to_message_id_unaffected():
    """Existing callers without reply_to_message_id are unaffected."""
    from src.api.routers.messages import send_message, SendMessageRequest

    db = _mock_db()
    captured: Dict[str, Any] = {}

    async def _fake_append(db_, session_id, role, content, author_id=None,
                           reply_to_message_id=None, thread_root_id=None, **kw):
        captured["reply_to_message_id"] = reply_to_message_id
        return 301

    session = _mock_session()

    with patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.messages.is_member", AsyncMock(return_value=True)), \
         patch("src.api.routers.messages.get_workspace_organization_id", AsyncMock(return_value="")), \
         patch("src.api.routers.messages.mention_candidates", AsyncMock(return_value=[])), \
         patch("src.api.routers.messages.resolve_mentions", return_value=[]), \
         patch("src.api.routers.messages.append_message", _fake_append), \
         patch("src.api.routers.messages.persist_mentions", AsyncMock()), \
         patch("src.api.routers.messages.touch_session", AsyncMock()), \
         patch("src.api.routers.messages.set_session_title", AsyncMock()), \
         patch("src.api.routers.messages.author_for", AsyncMock(return_value=None)), \
         patch("src.api.routers.messages.get_bus") as mock_bus, \
         patch("src.api.routers.messages._should_trigger_agent", return_value=False):

        mock_bus.return_value.publish = MagicMock()

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = SendMessageRequest(content="just a message")
        await send_message(
            session_id="sess-1",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert captured["reply_to_message_id"] is None


# ---------------------------------------------------------------------------
# SSE payload for send_message includes reply_to_message_id and thread_root_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_sse_payload_includes_reply_fields():
    """SSE payload from send_message includes reply_to_message_id and thread_root_id."""
    from src.api.routers.messages import send_message, SendMessageRequest

    db = _mock_db()
    published: list = []

    async def _fake_append(db_, session_id, role, content, author_id=None,
                           reply_to_message_id=None, thread_root_id=None, **kw):
        return 302

    session = _mock_session()

    with patch("src.api.routers.messages.get_session", AsyncMock(return_value=session)), \
         patch("src.api.routers.messages.is_member", AsyncMock(return_value=True)), \
         patch("src.api.routers.messages.get_workspace_organization_id", AsyncMock(return_value="")), \
         patch("src.api.routers.messages.mention_candidates", AsyncMock(return_value=[])), \
         patch("src.api.routers.messages.resolve_mentions", return_value=[]), \
         patch("src.api.routers.messages.append_message", _fake_append), \
         patch("src.api.routers.messages.persist_mentions", AsyncMock()), \
         patch("src.api.routers.messages.touch_session", AsyncMock()), \
         patch("src.api.routers.messages.set_session_title", AsyncMock()), \
         patch("src.api.routers.messages.author_for", AsyncMock(return_value=None)), \
         patch("src.api.routers.messages.get_bus") as mock_bus, \
         patch("src.api.routers.messages._should_trigger_agent", return_value=False):

        mock_bus.return_value.publish = lambda sid, payload: published.append(payload)

        request = MagicMock()
        identity = MagicMock()
        identity.user_id = "u1"
        identity.org_id = ""

        body = SendMessageRequest(content="hello", reply_to_message_id="77")
        await send_message(
            session_id="sess-1",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert len(published) == 1
    data = published[0]["data"]
    assert data["reply_to_message_id"] == "77"
    assert data["thread_root_id"] is None


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


def test_message_threads_router_registered():
    """The message_threads router is imported and included in the aggregate router module."""
    from src.api import router as router_module

    # The aggregate router module must import message_threads.
    assert hasattr(router_module, "message_threads"), (
        "message_threads not imported in src.api.router"
    )


def test_message_threads_post_and_get_routes_exist():
    """Both POST and GET routes for thread replies are defined in the message_threads router."""
    from src.api.routers.message_threads import router as mt_router

    reply_routes = [r for r in mt_router.routes if hasattr(r, "path") and "replies" in r.path]
    assert len(reply_routes) >= 2

    methods = set()
    for r in reply_routes:
        if hasattr(r, "methods"):
            methods.update(r.methods)

    assert "POST" in methods
    assert "GET" in methods
