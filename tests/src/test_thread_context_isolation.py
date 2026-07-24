"""Tests for T1 — G1: Thread-scope agent message context.

Covers:
- get_thread_messages_as_conversation: returns root + thread replies in
  conversation format; handles empty thread; omits inactive messages.
- post_thread_reply: uses get_thread_messages_as_conversation (not
  get_messages_as_conversation) so the agent sees only thread-scoped messages.
- _schedule_follow_up: uses thread-scoped history when thread_root_id is set;
  falls back to full-session history for top-level (no thread_root_id) turns.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub heavyweight deps not present in the test environment
# ---------------------------------------------------------------------------


def _inject_stubs():
    for mod in ("run_agent", "hermes_state", "plugins", "plugins.context", "plugins.skills"):
        if mod not in sys.modules:
            sys.modules[mod] = types.ModuleType(mod)

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


_inject_stubs()


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
    s = MagicMock()
    s.all.return_value = rows
    r = MagicMock()
    r.scalars.return_value = s
    return r


def _msg(
    id=1,
    session_id="sess-1",
    role="user",
    content="hello",
    active=True,
    thread_root_id=None,
    reply_to_message_id=None,
    tool_call_id=None,
    tool_name=None,
    tool_calls=None,
    finish_reason=None,
    reasoning=None,
    edited_at=None,
    image_ids=None,
):
    m = MagicMock()
    m.id = id
    m.session_id = session_id
    m.role = role
    m.content = content
    m.active = active
    m.thread_root_id = thread_root_id
    m.reply_to_message_id = reply_to_message_id
    m.tool_call_id = tool_call_id
    m.tool_name = tool_name
    m.tool_calls = tool_calls
    m.finish_reason = finish_reason
    m.reasoning = reasoning
    m.edited_at = edited_at
    m.image_ids = image_ids
    return m


def _mock_db_factory():
    db = _mock_db()

    class _CM:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_):
            pass

    def db_factory():
        return _CM()

    return db_factory, db


# ---------------------------------------------------------------------------
# get_thread_messages_as_conversation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_thread_messages_as_conversation_root_plus_replies():
    """Returns root message prepended to thread replies in conversation format."""
    from src.db.store import get_thread_messages_as_conversation

    root = _msg(id=10, role="user", content="Root message", thread_root_id=None)
    reply1 = _msg(id=11, role="assistant", content="Reply 1", thread_root_id=10)
    reply2 = _msg(id=12, role="user", content="Reply 2", thread_root_id=10)

    db = _mock_db()
    with patch("src.db.store.get_message", AsyncMock(return_value=root)):
        db.execute = AsyncMock(return_value=_scalars_result([reply1, reply2]))
        result = await get_thread_messages_as_conversation(db, "sess-1", 10)

    assert len(result) == 3
    assert result[0] == {"role": "user", "content": "Root message"}
    assert result[1] == {"role": "assistant", "content": "Reply 1"}
    assert result[2] == {"role": "user", "content": "Reply 2"}


@pytest.mark.asyncio
async def test_get_thread_messages_as_conversation_empty_thread_returns_root():
    """When no replies exist yet, result contains only the root message."""
    from src.db.store import get_thread_messages_as_conversation

    root = _msg(id=20, role="user", content="Just the root", thread_root_id=None)

    db = _mock_db()
    with patch("src.db.store.get_message", AsyncMock(return_value=root)):
        db.execute = AsyncMock(return_value=_scalars_result([]))
        result = await get_thread_messages_as_conversation(db, "sess-1", 20)

    assert result == [{"role": "user", "content": "Just the root"}]


@pytest.mark.asyncio
async def test_get_thread_messages_as_conversation_root_not_found():
    """When root message does not exist, result contains only the replies."""
    from src.db.store import get_thread_messages_as_conversation

    reply = _msg(id=31, role="user", content="Orphan reply", thread_root_id=30)

    db = _mock_db()
    with patch("src.db.store.get_message", AsyncMock(return_value=None)):
        db.execute = AsyncMock(return_value=_scalars_result([reply]))
        result = await get_thread_messages_as_conversation(db, "sess-1", 30)

    assert len(result) == 1
    assert result[0]["content"] == "Orphan reply"


@pytest.mark.asyncio
async def test_get_thread_messages_as_conversation_inactive_root_excluded():
    """Inactive root message is not included in the conversation."""
    from src.db.store import get_thread_messages_as_conversation

    root = _msg(id=40, role="user", content="Deleted root", active=False)
    reply = _msg(id=41, role="user", content="Reply", thread_root_id=40)

    db = _mock_db()
    with patch("src.db.store.get_message", AsyncMock(return_value=root)):
        db.execute = AsyncMock(return_value=_scalars_result([reply]))
        result = await get_thread_messages_as_conversation(db, "sess-1", 40)

    assert len(result) == 1
    assert result[0]["content"] == "Reply"


@pytest.mark.asyncio
async def test_get_thread_messages_as_conversation_preserves_tool_fields():
    """Tool-related fields are preserved in conversation format."""
    import json

    from src.db.store import get_thread_messages_as_conversation

    root = _msg(id=50, role="user", content="User asks")
    tool_reply = _msg(
        id=51,
        role="assistant",
        content="tool result",
        thread_root_id=50,
        tool_call_id="call-123",
        tool_name="my_tool",
        tool_calls=json.dumps([{"id": "call-123", "type": "function"}]),
        finish_reason="tool_calls",
        reasoning="thinking about it",
    )

    db = _mock_db()
    with patch("src.db.store.get_message", AsyncMock(return_value=root)):
        db.execute = AsyncMock(return_value=_scalars_result([tool_reply]))
        result = await get_thread_messages_as_conversation(db, "sess-1", 50)

    assert len(result) == 2
    reply_entry = result[1]
    assert reply_entry["tool_call_id"] == "call-123"
    assert reply_entry["tool_name"] == "my_tool"
    assert reply_entry["tool_calls"] == [{"id": "call-123", "type": "function"}]
    assert reply_entry["finish_reason"] == "tool_calls"
    assert reply_entry["reasoning"] == "thinking about it"


@pytest.mark.asyncio
async def test_get_thread_messages_as_conversation_null_content_coerced():
    """NULL content is coerced to empty string (same as get_messages_as_conversation)."""
    from src.db.store import get_thread_messages_as_conversation

    root = _msg(id=60, role="user", content=None)

    db = _mock_db()
    with patch("src.db.store.get_message", AsyncMock(return_value=root)):
        db.execute = AsyncMock(return_value=_scalars_result([]))
        result = await get_thread_messages_as_conversation(db, "sess-1", 60)

    assert result == [{"role": "user", "content": ""}]


# ---------------------------------------------------------------------------
# post_thread_reply — uses thread-scoped history for agent dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_thread_reply_uses_thread_scoped_history():
    """post_thread_reply builds agent history with get_thread_messages_as_conversation,
    not get_messages_as_conversation, so the agent only sees thread messages."""
    # Capture which history-building function is called.
    thread_history_calls: list = []
    session_history_calls: list = []

    async def mock_get_thread_messages_as_conversation(db, session_id, root_id):
        thread_history_calls.append((session_id, root_id))
        return [{"role": "user", "content": "thread root"}]

    async def mock_get_messages_as_conversation(db, session_id):
        session_history_calls.append(session_id)
        return [{"role": "user", "content": "full session"}]

    # Minimal session stub.
    session = MagicMock()
    session.workspace_id = "ws-1"
    session.kind = "thread"
    session.feature_id = "feat-1"
    session.model = "claude-3-5-sonnet-20241022"

    root_message = MagicMock()
    root_message.session_id = "sess-1"
    root_message.thread_root_id = None

    schedule_calls: list = []

    async def mock_schedule_agent_turn(**kwargs):
        schedule_calls.append(kwargs)
        return True

    with (
        patch("src.api.routers.message_threads.get_thread_messages_as_conversation", mock_get_thread_messages_as_conversation),
        patch("src.api.routers.message_threads.schedule_agent_turn", mock_schedule_agent_turn),
        patch("src.api.routers.message_threads.get_session", AsyncMock(return_value=session)),
        patch("src.api.routers.message_threads.authorize_thread_access", AsyncMock(return_value=(True, "org-1"))),
        patch("src.api.routers.message_threads.add_member", AsyncMock()),
        patch("src.api.routers.message_threads.resolve_mentions", MagicMock(return_value=[])),
        patch("src.api.routers.message_threads.persist_mentions", AsyncMock()),
        patch("src.api.routers.message_threads.touch_session", AsyncMock()),
        patch("src.api.routers.message_threads.update_session_model", AsyncMock()),
        patch("src.api.routers.message_threads.parse_mention_handles", MagicMock(return_value=["agent"])),
        patch("src.api.routers.message_threads.resolve_model", AsyncMock(return_value={"model": "claude-3-5-sonnet-20241022", "provider": None, "api_key": None, "base_url": None})),
        patch("src.api.routers.message_threads.append_message", AsyncMock(return_value=99)),
        patch("src.api.routers.message_threads.try_resolve_pending_clarify", MagicMock(return_value=False)),
        patch("src.api.routers.message_threads.get_bus", MagicMock(return_value=MagicMock(publish=MagicMock()))),
        patch("src.api.routers.message_threads.author_for", AsyncMock(return_value={})),
        patch("src.api.routers.message_threads.mention_candidates", AsyncMock(return_value=[])),
        patch("src.api.routers.message_threads._should_trigger_agent", MagicMock(return_value=True)),
    ):
        from src.api.routers import message_threads
        from src.db import Message

        async def mock_db_get(model, pk):
            if model is Message:
                return root_message
            return None

        db = _mock_db()
        db.get = AsyncMock(side_effect=mock_db_get)

        # Call post_thread_reply directly with a minimal request.
        from fastapi import Request

        request = MagicMock(spec=Request)
        request.app = MagicMock()
        request.app.state = MagicMock()
        request.app.state.db_session = MagicMock()

        body = message_threads.PostThreadReplyRequest(content="@agent help me")

        identity = MagicMock()
        identity.user_id = "u-1"
        identity.org_id = "org-1"

        await message_threads.post_thread_reply(
            session_id="sess-1",
            message_id="10",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    # Thread-scoped fetch must have been called; session-wide fetch must NOT.
    assert len(thread_history_calls) == 1
    assert thread_history_calls[0] == ("sess-1", 10)
    assert len(session_history_calls) == 0, "full-session history must not be fetched for thread dispatch"

    # The history passed to schedule_agent_turn must be the thread-scoped one.
    assert len(schedule_calls) == 1
    assert schedule_calls[0]["history"] == [{"role": "user", "content": "thread root"}]


# ---------------------------------------------------------------------------
# _schedule_follow_up — thread vs. top-level history branching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_follow_up_uses_thread_history_when_thread_root_id_set():
    """_schedule_follow_up fetches get_thread_messages_as_conversation when
    the pending turn has thread_root_id set."""
    from src.api.agent_dispatch import _schedule_follow_up

    loop = asyncio.get_running_loop()
    db_factory, _db = _mock_db_factory()

    thread_history_calls: list = []
    session_history_calls: list = []

    async def mock_thread_history(db, session_id, root_id):
        thread_history_calls.append((session_id, root_id))
        return [{"role": "user", "content": "thread only"}]

    async def mock_session_history(db, session_id):
        session_history_calls.append(session_id)
        return [{"role": "user", "content": "full session"}]

    pending = {
        "message": "@agent follow up",
        "workspace_id": "ws-1",
        "feature_id": "feat-1",
        "user_id": "u-1",
        "org_id": None,
        "model": "claude-3-5-sonnet-20241022",
        "db_factory": db_factory,
        "reply_to_message_id": 42,
        "thread_root_id": 10,
    }

    # The import is local inside _schedule_follow_up, so patch src.db.* directly.
    with (
        patch("src.db.get_thread_messages_as_conversation", mock_thread_history),
        patch("src.db.get_messages_as_conversation", mock_session_history),
        patch("src.db.touch_session", AsyncMock()),
        patch(
            "src.api.model_catalog.resolve_model",
            AsyncMock(return_value={"model": pending["model"], "provider": None, "api_key": None, "base_url": None}),
        ),
        patch("src.api.agent_dispatch.get_bus", MagicMock(return_value=MagicMock(publish=MagicMock()))),
        patch("src.api.agent_dispatch.asyncio.ensure_future") as mock_ensure,
    ):
        mock_ensure.return_value = MagicMock()
        await _schedule_follow_up("sess-1", pending, loop)
        if mock_ensure.called:
            coro = mock_ensure.call_args[0][0]
            coro.close()

    assert len(thread_history_calls) == 1, "thread history must be fetched for threaded pending turn"
    assert thread_history_calls[0] == ("sess-1", 10)
    assert len(session_history_calls) == 0, "full-session history must not be fetched for threaded turn"


@pytest.mark.asyncio
async def test_schedule_follow_up_uses_session_history_for_top_level_turn():
    """_schedule_follow_up fetches get_messages_as_conversation (not the thread
    variant) when thread_root_id is absent — regression guard for top-level turns."""
    from src.api.agent_dispatch import _schedule_follow_up

    loop = asyncio.get_running_loop()
    db_factory, _db = _mock_db_factory()

    thread_history_calls: list = []
    session_history_calls: list = []

    async def mock_thread_history(db, session_id, root_id):
        thread_history_calls.append((session_id, root_id))
        return []

    async def mock_session_history(db, session_id):
        session_history_calls.append(session_id)
        return [{"role": "user", "content": "full session msg"}]

    pending = {
        "message": "@agent top level",
        "workspace_id": "ws-1",
        "feature_id": "feat-1",
        "user_id": "u-1",
        "org_id": None,
        "model": "claude-3-5-sonnet-20241022",
        "db_factory": db_factory,
        # No thread_root_id — this is a top-level turn.
    }

    # The import is local inside _schedule_follow_up, so patch src.db.* directly.
    with (
        patch("src.db.get_thread_messages_as_conversation", mock_thread_history),
        patch("src.db.get_messages_as_conversation", mock_session_history),
        patch("src.db.touch_session", AsyncMock()),
        patch(
            "src.api.model_catalog.resolve_model",
            AsyncMock(return_value={"model": pending["model"], "provider": None, "api_key": None, "base_url": None}),
        ),
        patch("src.api.agent_dispatch.get_bus", MagicMock(return_value=MagicMock(publish=MagicMock()))),
        patch("src.api.agent_dispatch.asyncio.ensure_future") as mock_ensure,
    ):
        mock_ensure.return_value = MagicMock()
        await _schedule_follow_up("sess-1", pending, loop)
        if mock_ensure.called:
            coro = mock_ensure.call_args[0][0]
            coro.close()

    assert len(session_history_calls) == 1, "full-session history must be fetched for top-level pending turn"
    assert len(thread_history_calls) == 0, "thread history must NOT be fetched for top-level turn"
