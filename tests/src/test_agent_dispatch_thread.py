"""Unit tests for T4: agent dispatch thread_root_id / reply_to_message_id propagation.

Covers:
- schedule_agent_turn accepts reply_to_message_id / thread_root_id params.
- _backfill_assistant propagates thread context to append_message when backfill
  is needed (no assistant row found after last user message).
- _backfill_assistant skips the write when the mirror already captured the turn.
- _run_agent_turn_async propagates thread context to the cancelled-partial
  append_message call.
- Agent reply outside a thread has both fields NULL (backward-compatible).
- schedule_agent_turn without the new kwargs is source-compatible (default None).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_db_factory():
    """Return a db_factory callable whose context manager yields a mock db."""
    db = MagicMock()
    db.add = MagicMock()
    db.execute = AsyncMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.delete = AsyncMock()

    class _CM:
        async def __aenter__(self):
            return db

        async def __aexit__(self, *_):
            pass

    def db_factory():
        return _CM()

    return db_factory, db


# ---------------------------------------------------------------------------
# _backfill_assistant — thread context propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_backfill_assistant_propagates_thread_context_when_writing():
    """_backfill_assistant writes with reply_to/thread_root when no assistant exists.

    A threaded call (thread_root_id set) must check for an existing assistant row
    via get_thread_replies, not the thread-blind get_session_messages (which
    filters to thread_root_id IS NULL and so can never see a thread reply's own
    mirrored row — see the regression test below for what happens when it's used
    incorrectly).
    """
    from src.api.agent_dispatch import _backfill_assistant

    captured = []

    async def mock_get_thread_replies(db, session_id, root_message_id, since=None):
        # One user message, no assistant message → backfill should fire.
        return [{"role": "user", "content": "hey agent"}]

    async def mock_append(db, session_id, **kwargs):
        captured.append(kwargs)
        return 99

    with patch("src.db.get_thread_replies", mock_get_thread_replies):
        with patch("src.db.store.append_message", mock_append):
            db_factory, _ = _mock_db_factory()
            await _backfill_assistant(
                db_factory,
                "sess-1",
                "agent answer",
                reply_to_message_id=10,
                thread_root_id=5,
            )

    assert len(captured) == 1
    assert captured[0]["reply_to_message_id"] == 10
    assert captured[0]["thread_root_id"] == 5
    assert captured[0]["role"] == "assistant"
    assert captured[0]["content"] == "agent answer"


@pytest.mark.asyncio
async def test_backfill_assistant_skips_when_assistant_already_present():
    """_backfill_assistant does NOT write when the mirror already captured the turn."""
    from src.api.agent_dispatch import _backfill_assistant

    append_calls = []

    async def mock_get_thread_replies(db, session_id, root_message_id, since=None):
        # user + assistant already there — mirror captured the turn.
        return [
            {"role": "user", "content": "hey"},
            {"role": "assistant", "content": "already here"},
        ]

    async def mock_append(db, session_id, **kwargs):
        append_calls.append(kwargs)
        return 100

    with patch("src.db.get_thread_replies", mock_get_thread_replies):
        with patch("src.db.store.append_message", mock_append):
            db_factory, _ = _mock_db_factory()
            await _backfill_assistant(
                db_factory,
                "sess-1",
                "new content",
                reply_to_message_id=10,
                thread_root_id=5,
            )

    assert append_calls == [], "backfill must not write when mirror already captured it"


@pytest.mark.asyncio
async def test_backfill_assistant_thread_reply_ignores_main_channel_query():
    """Regression test: a threaded call must not consult get_session_messages at all.

    get_session_messages filters to thread_root_id IS NULL, so it can never see a
    thread reply's mirrored assistant row. Using it as the existence check for a
    threaded turn meant the guard always concluded "no assistant row yet" and
    unconditionally wrote a duplicate — a 100%-reproducible double-persisted
    assistant message for every single thread reply, not a race.
    """
    from src.api.agent_dispatch import _backfill_assistant

    append_calls = []

    async def mock_get_session_messages(db, session_id):
        # The main-channel view has no idea this thread turn happened.
        return []

    async def mock_get_thread_replies(db, session_id, root_message_id, since=None):
        # But the thread itself already has the mirror's assistant row.
        return [
            {"role": "user", "content": "hey"},
            {"role": "assistant", "content": "already here"},
        ]

    async def mock_append(db, session_id, **kwargs):
        append_calls.append(kwargs)
        return 100

    with patch("src.db.get_session_messages", mock_get_session_messages):
        with patch("src.db.get_thread_replies", mock_get_thread_replies):
            with patch("src.db.store.append_message", mock_append):
                db_factory, _ = _mock_db_factory()
                await _backfill_assistant(
                    db_factory,
                    "sess-1",
                    "new content",
                    reply_to_message_id=10,
                    thread_root_id=5,
                )

    assert append_calls == [], "threaded backfill must consult get_thread_replies, not get_session_messages"


@pytest.mark.asyncio
async def test_backfill_assistant_no_thread_context_defaults_null():
    """_backfill_assistant without thread args writes with NULL thread fields."""
    from src.api.agent_dispatch import _backfill_assistant

    captured = []

    async def mock_get_session_messages(db, session_id):
        return [{"role": "user", "content": "hi"}]

    async def mock_append(db, session_id, **kwargs):
        captured.append(kwargs)
        return 55

    with patch("src.db.get_session_messages", mock_get_session_messages):
        with patch("src.db.store.append_message", mock_append):
            db_factory, _ = _mock_db_factory()
            await _backfill_assistant(db_factory, "sess-1", "hi back")

    assert len(captured) == 1
    assert captured[0]["reply_to_message_id"] is None
    assert captured[0]["thread_root_id"] is None


# ---------------------------------------------------------------------------
# schedule_agent_turn — signature compatibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_agent_turn_accepts_thread_params():
    """schedule_agent_turn accepts reply_to_message_id / thread_root_id without error."""
    from src.api.agent_dispatch import schedule_agent_turn

    loop = asyncio.get_running_loop()
    db_factory, _ = _mock_db_factory()

    with patch("src.api.agent_dispatch.asyncio.ensure_future") as mock_ensure:
        mock_ensure.return_value = MagicMock()
        with patch("src.api.agent_dispatch.get_bus") as mock_bus:
            mock_bus.return_value.publish = MagicMock()
            result = await schedule_agent_turn(
                session_id="sess-thread",
                message="@agent help",
                history=[],
                workspace_id="ws-1",
                feature_id="feat-1",
                user_id="u-1",
                model="claude-3-5-sonnet-20241022",
                provider=None,
                api_key=None,
                base_url=None,
                db_factory=db_factory,
                loop=loop,
                reply_to_message_id=42,
                thread_root_id=10,
            )

    # Must close the uncompleted coroutine to avoid warnings.
    coro = mock_ensure.call_args[0][0]
    coro.close()
    assert result is True


@pytest.mark.asyncio
async def test_schedule_agent_turn_without_thread_params_is_backward_compatible():
    """schedule_agent_turn without new kwargs remains source-compatible."""
    from src.api.agent_dispatch import schedule_agent_turn

    loop = asyncio.get_running_loop()
    db_factory, _ = _mock_db_factory()

    with patch("src.api.agent_dispatch.asyncio.ensure_future") as mock_ensure:
        mock_ensure.return_value = MagicMock()
        with patch("src.api.agent_dispatch.get_bus") as mock_bus:
            mock_bus.return_value.publish = MagicMock()
            # Call without reply_to_message_id or thread_root_id — must not raise.
            result = await schedule_agent_turn(
                session_id="sess-compat",
                message="hello",
                history=[],
                workspace_id="ws-1",
                feature_id="feat-1",
                user_id="u-1",
                model="claude-3-5-sonnet-20241022",
                provider=None,
                api_key=None,
                base_url=None,
                db_factory=db_factory,
                loop=loop,
            )

    coro = mock_ensure.call_args[0][0]
    coro.close()
    assert result is True


# ---------------------------------------------------------------------------
# schedule_agent_turn — coalescing preserves thread context for the follow-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_agent_turn_coalesce_preserves_thread_context():
    """When a turn is already in flight, the coalesced pending entry retains
    reply_to_message_id / thread_root_id so the eventual follow-up turn (run
    via _schedule_follow_up) still persists as a thread reply instead of
    silently reverting to a top-level channel message."""
    import src.api.agent_dispatch as agent_dispatch
    from src.api.agent_dispatch import ActiveRun, schedule_agent_turn

    loop = asyncio.get_running_loop()
    db_factory, _ = _mock_db_factory()
    session_id = "sess-coalesce"

    agent_dispatch._active_runs[session_id] = ActiveRun(
        run_id="existing-run", task=None, triggered_by="u-0"
    )
    try:
        result = await schedule_agent_turn(
            session_id=session_id,
            message="@agent second message",
            history=[],
            workspace_id="ws-1",
            feature_id="feat-1",
            user_id="u-1",
            model="claude-3-5-sonnet-20241022",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=db_factory,
            loop=loop,
            reply_to_message_id=42,
            thread_root_id=10,
        )

        assert result is False
        pending = agent_dispatch._pending_agent_turns[session_id]
        assert pending["reply_to_message_id"] == 42
        assert pending["thread_root_id"] == 10
    finally:
        agent_dispatch._active_runs.pop(session_id, None)
        agent_dispatch._pending_agent_turns.pop(session_id, None)


@pytest.mark.asyncio
async def test_schedule_follow_up_passes_thread_context_to_run_agent_turn_async():
    """_schedule_follow_up forwards the coalesced pending entry's thread context
    into _run_agent_turn_async, so the follow-up turn's mirrored assistant rows
    are still tagged as replies to the correct thread."""
    from src.api.agent_dispatch import _schedule_follow_up

    loop = asyncio.get_running_loop()
    db_factory, db = _mock_db_factory()
    session_id = "sess-follow-up"

    pending = {
        "message": "@agent second message",
        "workspace_id": "ws-1",
        "feature_id": "feat-1",
        "user_id": "u-1",
        "org_id": None,
        "model": "claude-3-5-sonnet-20241022",
        "db_factory": db_factory,
        "reply_to_message_id": 42,
        "thread_root_id": 10,
    }

    with (
        patch("src.db.get_messages_as_conversation", AsyncMock(return_value=[])),
        patch("src.db.touch_session", AsyncMock()),
        patch(
            "src.api.model_catalog.resolve_model",
            AsyncMock(
                return_value={
                    "model": pending["model"],
                    "provider": None,
                    "api_key": None,
                    "base_url": None,
                }
            ),
        ),
        patch("src.api.agent_dispatch.get_bus") as mock_bus,
        patch("src.api.agent_dispatch.asyncio.ensure_future") as mock_ensure,
    ):
        mock_bus.return_value.publish = MagicMock()
        mock_ensure.return_value = MagicMock()

        await _schedule_follow_up(session_id, pending, loop)

    coro = mock_ensure.call_args[0][0]
    # A coroutine's frame locals hold its bound arguments as soon as it's created
    # (before ever running) — inspect before close() clears cr_frame to None.
    frame_locals = coro.cr_frame.f_locals if coro.cr_frame else {}
    assert frame_locals.get("reply_to_message_id") == 42
    assert frame_locals.get("thread_root_id") == 10
    coro.close()


@pytest.mark.asyncio
async def test_run_agent_turn_async_cancelled_partial_has_thread_context():
    """Cancelled partial message is persisted with the thread context."""
    from src.api.agent_dispatch import _run_agent_turn_async

    captured = []

    async def mock_append(db, session_id, **kwargs):
        captured.append(kwargs)
        return 88

    # A translator that reports a non-empty partial on mark_stopped.
    translator = MagicMock()
    translator.mark_stopped.return_value = "partial agent text"
    translator.on_delta = MagicMock()

    loop = asyncio.get_running_loop()
    db_factory, _ = _mock_db_factory()

    async def mock_run_in_executor(executor, fn):
        raise asyncio.CancelledError()

    with patch("src.api.agent_dispatch.emit_turn_cost", new_callable=AsyncMock):
        with patch("src.db.store.append_message", mock_append):
            with patch("src.api.agent_dispatch.get_bus") as mock_bus:
                mock_bus.return_value.publish = MagicMock()
                with patch.object(loop, "run_in_executor", mock_run_in_executor):
                    try:
                        await _run_agent_turn_async(
                            run_id="run-1",
                            session_id="sess-cancel",
                            triggered_by="u-1",
                            db_factory=db_factory,
                            loop=loop,
                            translator=translator,
                            message="hey",
                            history=[],
                            workspace_id="ws-1",
                            feature_id="feat-1",
                            user_id="u-1",
                            model="claude-3-5-sonnet-20241022",
                            provider=None,
                            api_key=None,
                            base_url=None,
                            reply_to_message_id=20,
                            thread_root_id=15,
                        )
                    except asyncio.CancelledError:
                        pass  # Some paths re-raise, some don't — both are valid.

    assert len(captured) >= 1
    assert captured[0]["reply_to_message_id"] == 20
    assert captured[0]["thread_root_id"] == 15
    assert captured[0]["finish_reason"] == "stopped"


@pytest.mark.asyncio
async def test_run_agent_turn_async_cancelled_partial_no_thread_is_null():
    """Cancelled partial without thread context has NULL thread fields."""
    from src.api.agent_dispatch import _run_agent_turn_async

    captured = []

    async def mock_append(db, session_id, **kwargs):
        captured.append(kwargs)
        return 77

    translator = MagicMock()
    translator.mark_stopped.return_value = "partial text no thread"
    translator.on_delta = MagicMock()

    loop = asyncio.get_running_loop()
    db_factory, _ = _mock_db_factory()

    async def mock_run_in_executor(executor, fn):
        raise asyncio.CancelledError()

    with patch("src.api.agent_dispatch.emit_turn_cost", new_callable=AsyncMock):
        with patch("src.db.store.append_message", mock_append):
            with patch("src.api.agent_dispatch.get_bus") as mock_bus:
                mock_bus.return_value.publish = MagicMock()
                with patch.object(loop, "run_in_executor", mock_run_in_executor):
                    try:
                        await _run_agent_turn_async(
                            run_id="run-2",
                            session_id="sess-cancel2",
                            triggered_by="u-1",
                            db_factory=db_factory,
                            loop=loop,
                            translator=translator,
                            message="hi",
                            history=[],
                            workspace_id="ws-1",
                            feature_id="feat-1",
                            user_id="u-1",
                            model="claude-3-5-sonnet-20241022",
                            provider=None,
                            api_key=None,
                            base_url=None,
                        )
                    except asyncio.CancelledError:
                        pass

    assert len(captured) >= 1
    assert captured[0]["reply_to_message_id"] is None
    assert captured[0]["thread_root_id"] is None
