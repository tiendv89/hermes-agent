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
    """_backfill_assistant writes with reply_to/thread_root when no assistant exists."""
    from src.api.agent_dispatch import _backfill_assistant

    captured = []

    async def mock_get_session_messages(db, session_id):
        # One user message, no assistant message → backfill should fire.
        return [{"role": "user", "content": "hey agent"}]

    async def mock_append(db, session_id, **kwargs):
        captured.append(kwargs)
        return 99

    with patch("src.db.get_session_messages", mock_get_session_messages):
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

    async def mock_get_session_messages(db, session_id):
        # user + assistant already there — mirror captured the turn.
        return [
            {"role": "user", "content": "hey"},
            {"role": "assistant", "content": "already here"},
        ]

    async def mock_append(db, session_id, **kwargs):
        append_calls.append(kwargs)
        return 100

    with patch("src.db.get_session_messages", mock_get_session_messages):
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
# _run_agent_turn_async — cancelled-partial propagates thread context
# ---------------------------------------------------------------------------


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
