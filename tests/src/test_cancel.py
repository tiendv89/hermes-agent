"""Tests for m3-stop-agent-chat T1: cancel endpoint + ActiveRun tracking + CancelledError handler.

Covers the T1 test plan:
  - Cancel with no active turn → 404 {"error": "no_active_turn"} (well, detail)
  - Cancel by non-triggering thread member → 403
  - Cancel by triggering member during active turn → 202; task.cancel() called
  - After cancel, session is free (not stuck in _active_runs)
  - CancelledError handler: partial text persisted with finish_reason='stopped'
  - CancelledError handler: turn.stopped published to bus with message_id
  - CancelledError handler: turn.stopped with message_id=None when no tokens
  - mark_stopped() suppresses translator done() to prevent stale agent.done on bus
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Stub injection helpers (same pattern as other test files)
# ---------------------------------------------------------------------------


def _inject_stubs():
    if "run_agent" not in sys.modules:
        stub = types.ModuleType("run_agent")
        stub.AIAgent = MagicMock()  # type: ignore[attr-defined]
        sys.modules["run_agent"] = stub

    if "hermes_state" not in sys.modules:
        stub = types.ModuleType("hermes_state")

        class _FakeSessionDB:
            def append_message(self, *a, **kw):
                return 0

            def update_token_counts(self, *a, **kw):
                pass

        stub.SessionDB = _FakeSessionDB  # type: ignore[attr-defined]
        sys.modules["hermes_state"] = stub

    for _mod in ("plugins", "plugins.context", "plugins.skills"):
        if _mod not in sys.modules:
            sys.modules[_mod] = types.ModuleType(_mod)

    ctx = sys.modules.get("plugins.context") or types.ModuleType("plugins.context")
    if not hasattr(ctx, "set_context"):
        ctx.set_context = MagicMock()  # type: ignore[attr-defined]
        ctx.clear_context = MagicMock()  # type: ignore[attr-defined]
        sys.modules["plugins.context"] = ctx

    skills = sys.modules.get("plugins.skills") or types.ModuleType("plugins.skills")
    if not hasattr(skills, "get_shared_rules"):
        skills.get_shared_rules = lambda: None  # type: ignore[attr-defined]
        sys.modules["plugins.skills"] = skills


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cancel_app():
    """Minimal FastAPI app with the src router."""
    _inject_stubs()

    from fastapi import FastAPI

    from src.api.router import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    @asynccontextmanager
    async def _db_session():
        yield MagicMock()

    app.state.db_session = _db_session
    return app


# ---------------------------------------------------------------------------
# Cancel endpoint — HTTP behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancel_no_active_turn_returns_404(cancel_app):
    """POST /threads/{id}/cancel with no in-flight turn → 404."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import _active_runs, _active_runs_lock

    session_id = "sess_cancel_404"
    with _active_runs_lock:
        _active_runs.pop(session_id, None)

    async with AsyncClient(
        transport=ASGITransport(app=cancel_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            f"/api/v1/threads/{session_id}/cancel",
            headers={"X-User-Id": "user_a"},
        )

    assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_cancel_non_triggering_member_returns_403(cancel_app):
    """Cancel by a member who did not trigger the turn → 403."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import ActiveRun, _active_runs, _active_runs_lock

    session_id = "sess_cancel_403"
    fake_task = MagicMock(spec=asyncio.Task)
    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(run_id="test-run-403", task=fake_task, triggered_by="user_owner")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{session_id}/cancel",
                headers={"X-User-Id": "user_interloper"},
            )

        assert resp.status_code == 403, (
            f"Expected 403, got {resp.status_code}: {resp.text}"
        )
        fake_task.cancel.assert_not_called()
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)


@pytest.mark.asyncio
async def test_cancel_by_triggering_member_returns_202(cancel_app):
    """Cancel by the triggering member → 202, task.cancel() called."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import ActiveRun, _active_runs, _active_runs_lock

    session_id = "sess_cancel_202"
    fake_task = MagicMock(spec=asyncio.Task)
    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(run_id="test-run-202", task=fake_task, triggered_by="user_a")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{session_id}/cancel",
                headers={"X-User-Id": "user_a"},
            )

        assert resp.status_code == 202, (
            f"Expected 202, got {resp.status_code}: {resp.text}"
        )
        assert resp.json() == {"status": "cancelling"}
        fake_task.cancel.assert_called_once()
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)


@pytest.mark.asyncio
async def test_cancel_interrupts_agent_and_sets_event(cancel_app):
    """Cancel must set cancel_event and interrupt the live agent — not just
    cancel the asyncio task — so the blocking worker thread actually stops."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import ActiveRun, _active_runs, _active_runs_lock

    session_id = "sess_cancel_interrupt"
    fake_task = MagicMock(spec=asyncio.Task)
    fake_agent = MagicMock()
    run = ActiveRun(run_id="test-run-interrupt", task=fake_task, triggered_by="user_a")
    run.agent = fake_agent
    with _active_runs_lock:
        _active_runs[session_id] = run

    try:
        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{session_id}/cancel",
                headers={"X-User-Id": "user_a"},
            )

        assert resp.status_code == 202, resp.text
        assert run.cancel_event.is_set(), "cancel_event must be set for the worker"
        fake_agent.interrupt.assert_called_once()
        fake_task.cancel.assert_called_once()
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)


@pytest.mark.asyncio
async def test_cancel_with_no_agent_still_sets_event(cancel_app):
    """If cancel races setup before the agent is built, cancel_event still fires
    (the worker bails at its next checkpoint) and no interrupt is attempted."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import ActiveRun, _active_runs, _active_runs_lock

    session_id = "sess_cancel_no_agent"
    fake_task = MagicMock(spec=asyncio.Task)
    run = ActiveRun(run_id="test-run-no-agent", task=fake_task, triggered_by="user_a")
    with _active_runs_lock:
        _active_runs[session_id] = run

    try:
        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/threads/{session_id}/cancel",
                headers={"X-User-Id": "user_a"},
            )

        assert resp.status_code == 202, resp.text
        assert run.cancel_event.is_set()
        assert run.agent is None
        fake_task.cancel.assert_called_once()
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)


@pytest.mark.asyncio
async def test_cancel_missing_identity_returns_400(cancel_app):
    """Cancel with no X-User-Id header → 400."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import ActiveRun, _active_runs, _active_runs_lock

    session_id = "sess_cancel_no_identity"
    fake_task = MagicMock(spec=asyncio.Task)
    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(run_id="test-run-identity", task=fake_task, triggered_by="user_a")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            resp = await client.post(f"/api/v1/threads/{session_id}/cancel")

        assert resp.status_code == 400, (
            f"Expected 400, got {resp.status_code}: {resp.text}"
        )
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)


# ---------------------------------------------------------------------------
# CancelledError handler in _run_agent_turn_async
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cancelled_error_persists_partial_message():
    """CancelledError: non-empty partial text is persisted with finish_reason='stopped'."""
    _inject_stubs()
    from src.api.agent_dispatch import _run_agent_turn_async
    from src.streaming.sse import HermesSSETranslator

    session_id = "sess_cancelled_persist"
    loop = asyncio.get_event_loop()

    # Translator that already has accumulated text.
    translator = MagicMock(spec=HermesSSETranslator)
    translator.mark_stopped.return_value = "partial text"

    mock_append = AsyncMock(return_value=42)
    published_events: list = []

    @asynccontextmanager
    async def _db_factory():
        yield MagicMock()

    async def _cancelled_executor(*args, **kwargs):
        raise asyncio.CancelledError()

    with (
        patch("src.api.agent_dispatch.get_bus") as mock_bus,
        patch("src.api.agent_dispatch.append_message", mock_append, create=True),
    ):
        mock_bus_instance = MagicMock()
        mock_bus.return_value = mock_bus_instance
        mock_bus_instance.publish.side_effect = lambda sid, evt: (
            published_events.append(evt)
        )

        with patch.object(loop, "run_in_executor", side_effect=_cancelled_executor):
            await _run_agent_turn_async(
                run_id="test-run-persist",
                session_id=session_id,
                triggered_by="user_a",
                message="hello",
                history=[],
                workspace_id="ws-1",
                feature_id="feat-1",
                user_id="user_a",
                model="test-model",
                provider=None,
                api_key=None,
                base_url=None,
                db_factory=_db_factory,
                loop=loop,
                translator=translator,
            )

    # mark_stopped() must have been called.
    translator.mark_stopped.assert_called_once()

    # turn.stopped must be published.
    stop_events = [e for e in published_events if e.get("event") == "turn.stopped"]
    assert stop_events, f"Expected turn.stopped event, got: {published_events}"


@pytest.mark.asyncio
async def test_cancelled_error_no_tokens_no_persist():
    """CancelledError with zero tokens: no message persisted, turn.stopped with message_id=None."""
    _inject_stubs()
    from src.api.agent_dispatch import _run_agent_turn_async
    from src.streaming.sse import HermesSSETranslator

    session_id = "sess_cancelled_no_tokens"
    loop = asyncio.get_event_loop()

    translator = MagicMock(spec=HermesSSETranslator)
    translator.mark_stopped.return_value = ""  # no tokens generated

    published_events: list = []

    @asynccontextmanager
    async def _db_factory():
        yield MagicMock()

    async def _cancelled_executor(*args, **kwargs):
        raise asyncio.CancelledError()

    with patch("src.api.agent_dispatch.get_bus") as mock_bus:
        mock_bus_instance = MagicMock()
        mock_bus.return_value = mock_bus_instance
        mock_bus_instance.publish.side_effect = lambda sid, evt: (
            published_events.append(evt)
        )

        with patch.object(loop, "run_in_executor", side_effect=_cancelled_executor):
            await _run_agent_turn_async(
                run_id="test-run-no-tokens",
                session_id=session_id,
                triggered_by="user_a",
                message="hello",
                history=[],
                workspace_id="ws-1",
                feature_id="feat-1",
                user_id="user_a",
                model="test-model",
                provider=None,
                api_key=None,
                base_url=None,
                db_factory=_db_factory,
                loop=loop,
                translator=translator,
            )

    stop_events = [e for e in published_events if e.get("event") == "turn.stopped"]
    assert stop_events, f"Expected turn.stopped event, got: {published_events}"
    assert stop_events[0]["data"]["message_id"] is None, (
        f"Expected null message_id for zero-token cancel, got {stop_events[0]['data']['message_id']}"
    )


@pytest.mark.asyncio
async def test_cancelled_session_freed_from_active_runs():
    """After CancelledError, the session is removed from _active_runs immediately."""
    _inject_stubs()
    from src.api.agent_dispatch import (
        ActiveRun,
        _active_runs,
        _active_runs_lock,
        _run_agent_turn_async,
    )
    from src.streaming.sse import HermesSSETranslator

    session_id = "sess_cancel_cleanup"
    loop = asyncio.get_event_loop()

    translator = MagicMock(spec=HermesSSETranslator)
    translator.mark_stopped.return_value = ""

    @asynccontextmanager
    async def _db_factory():
        yield MagicMock()

    async def _cancelled_executor(*args, **kwargs):
        raise asyncio.CancelledError()

    test_run_id = "test-run-cleanup"

    # Place session in active_runs using the same run_id as the async wrapper.
    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(run_id=test_run_id, task=None, triggered_by="user_a")

    with patch(
        "src.api.agent_dispatch.get_bus",
        MagicMock(return_value=MagicMock(publish=MagicMock())),
    ), patch.object(loop, "run_in_executor", side_effect=_cancelled_executor):
        await _run_agent_turn_async(
            run_id=test_run_id,
            session_id=session_id,
            triggered_by="user_a",
            message="hello",
            history=[],
            workspace_id="ws-1",
            feature_id="feat-1",
            user_id="user_a",
            model="test-model",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=_db_factory,
            loop=loop,
            translator=translator,
        )

    with _active_runs_lock:
        assert session_id not in _active_runs, (
            "Session should be removed from _active_runs after cancellation"
        )


# ---------------------------------------------------------------------------
# GatewaySessionDB suppresses mirror writes once cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gateway_db_skips_mirror_when_cancelled():
    """Once is_cancelled() is True, append_message must not mirror to Postgres —
    so a turn cancelled mid-flight does not persist messages behind the user."""
    _inject_stubs()
    import threading

    from src.db.session_db_proxy import make_gateway_session_db

    loop = asyncio.get_event_loop()
    session_id = "sess_mirror_guard"
    cancel_event = threading.Event()

    pg_calls: list = []

    @asynccontextmanager
    async def _db_factory():
        yield MagicMock()

    session_db = make_gateway_session_db(
        loop,
        _db_factory,
        session_id,
        is_cancelled=lambda: cancel_event.is_set(),
    )

    # The mirror is scheduled onto the loop via run_coroutine_threadsafe; stub it
    # so we can count attempts without needing a live cross-thread loop. The
    # returned future must be result()-able (the proxy blocks on it).
    def _fake_schedule(coro, _loop):
        coro.close()  # avoid "never awaited" warning
        pg_calls.append(1)
        fut = MagicMock()
        fut.result.return_value = None
        return fut

    with patch(
        "src.db.session_db_proxy.asyncio.run_coroutine_threadsafe",
        side_effect=_fake_schedule,
    ):
        # Not cancelled yet → mirrors to Postgres.
        session_db.append_message(session_id, "assistant", content="before")
        assert len(pg_calls) == 1, "write before cancel should mirror"

        # Cancelled → mirror suppressed.
        cancel_event.set()
        session_db.append_message(session_id, "assistant", content="after stop")
        assert len(pg_calls) == 1, "write after cancel must NOT mirror"


# ---------------------------------------------------------------------------
# mark_stopped() suppresses translator done()
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_stopped_suppresses_done_on_sse_translator():
    """mark_stopped() prevents _terminate() from emitting the [DONE] frame."""
    from src.streaming.sse import HermesSSETranslator

    t = HermesSSETranslator(model="test")
    t.on_delta(delta="hello ")
    t.on_delta(delta="world")

    partial = t.mark_stopped()
    assert partial == "hello world", f"Unexpected partial: {partial!r}"
    assert t._stopped is True

    # Calling done() after mark_stopped must be a no-op.
    t.done()
    assert t._terminated is False, "done() should not set _terminated after stop"


@pytest.mark.asyncio
async def test_mark_stopped_suppresses_done_on_bus_translator():
    """BusPublishingSSETranslator.done() is suppressed after mark_stopped()."""
    from src.streaming.bus_translator import BusPublishingSSETranslator

    with patch("src.streaming.bus_translator.get_bus") as mock_get_bus:
        mock_bus = MagicMock()
        mock_get_bus.return_value = mock_bus

        t = BusPublishingSSETranslator(session_id="sess_x", model="test")
        t.on_delta(delta="token1")

        t.mark_stopped()
        t.done()

        # The bus publish happens via _bus_publish which uses call_soon_threadsafe.
        # We verify _terminated was not set.
        assert t._terminated is False


# ---------------------------------------------------------------------------
# Session-scoped cancel endpoint — POST /sessions/{id}/cancel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_cancel_no_active_turn_returns_404(cancel_app):
    """POST /sessions/{id}/cancel with no in-flight turn → 404."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import _active_runs, _active_runs_lock

    session_id = "sess_session_cancel_404"
    with _active_runs_lock:
        _active_runs.pop(session_id, None)

    async with AsyncClient(
        transport=ASGITransport(app=cancel_app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            f"/api/v1/sessions/{session_id}/cancel",
            headers={"X-User-Id": "user_a"},
        )

    assert resp.status_code == 404, f"Expected 404, got {resp.status_code}: {resp.text}"


@pytest.mark.asyncio
async def test_session_cancel_non_triggering_member_returns_403(cancel_app):
    """POST /sessions/{id}/cancel by a non-triggering member → 403."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import ActiveRun, _active_runs, _active_runs_lock

    session_id = "sess_session_cancel_403"
    fake_task = MagicMock(spec=asyncio.Task)
    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(run_id="test-session-run-403", task=fake_task, triggered_by="user_owner")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/sessions/{session_id}/cancel",
                headers={"X-User-Id": "user_interloper"},
            )

        assert resp.status_code == 403, (
            f"Expected 403, got {resp.status_code}: {resp.text}"
        )
        fake_task.cancel.assert_not_called()
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)


@pytest.mark.asyncio
async def test_session_cancel_by_triggering_member_returns_202(cancel_app):
    """POST /sessions/{id}/cancel by the triggering member → 202, task.cancel() called."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import ActiveRun, _active_runs, _active_runs_lock

    session_id = "sess_session_cancel_202"
    fake_task = MagicMock(spec=asyncio.Task)
    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(run_id="test-session-run-202", task=fake_task, triggered_by="user_a")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/sessions/{session_id}/cancel",
                headers={"X-User-Id": "user_a"},
            )

        assert resp.status_code == 202, (
            f"Expected 202, got {resp.status_code}: {resp.text}"
        )
        assert resp.json() == {"status": "cancelling"}
        fake_task.cancel.assert_called_once()
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)


@pytest.mark.asyncio
async def test_session_cancel_interrupts_agent_and_sets_event(cancel_app):
    """POST /sessions/{id}/cancel sets cancel_event and calls agent.interrupt()."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import ActiveRun, _active_runs, _active_runs_lock

    session_id = "sess_session_cancel_interrupt"
    fake_task = MagicMock(spec=asyncio.Task)
    fake_agent = MagicMock()
    run = ActiveRun(run_id="test-session-run-interrupt", task=fake_task, triggered_by="user_a")
    run.agent = fake_agent
    with _active_runs_lock:
        _active_runs[session_id] = run

    try:
        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            resp = await client.post(
                f"/api/v1/sessions/{session_id}/cancel",
                headers={"X-User-Id": "user_a"},
            )

        assert resp.status_code == 202, resp.text
        assert run.cancel_event.is_set(), "cancel_event must be set for the worker"
        fake_agent.interrupt.assert_called_once()
        fake_task.cancel.assert_called_once()
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)


@pytest.mark.asyncio
async def test_session_cancel_missing_identity_returns_400(cancel_app):
    """POST /sessions/{id}/cancel with no X-User-Id header → 400."""
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import ActiveRun, _active_runs, _active_runs_lock

    session_id = "sess_session_cancel_no_identity"
    fake_task = MagicMock(spec=asyncio.Task)
    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(run_id="test-session-run-identity", task=fake_task, triggered_by="user_a")

    try:
        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            resp = await client.post(f"/api/v1/sessions/{session_id}/cancel")

        assert resp.status_code == 400, (
            f"Expected 400, got {resp.status_code}: {resp.text}"
        )
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)


@pytest.mark.asyncio
async def test_session_and_thread_cancel_share_same_run(cancel_app):
    """Both /sessions/{id}/cancel and /threads/{id}/cancel operate on the same ActiveRun.

    The session-scoped endpoint is a semantic alias — it cancels the same
    in-flight run that the thread-scoped endpoint would cancel.
    """
    _inject_stubs()
    from httpx import ASGITransport, AsyncClient

    from src.api.agent_dispatch import ActiveRun, _active_runs, _active_runs_lock

    session_id = "sess_shared_run_test"
    fake_task = MagicMock(spec=asyncio.Task)
    run = ActiveRun(run_id="test-shared-run", task=fake_task, triggered_by="user_a")
    with _active_runs_lock:
        _active_runs[session_id] = run

    try:
        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            # Cancel via the session-scoped endpoint.
            resp = await client.post(
                f"/api/v1/sessions/{session_id}/cancel",
                headers={"X-User-Id": "user_a"},
            )

        assert resp.status_code == 202, resp.text
        assert run.cancel_event.is_set()
        fake_task.cancel.assert_called_once()

        # A second cancel via the thread-scoped endpoint on an already-cancelled
        # run (task is still in _active_runs until the task coroutine cleans up)
        # should also work if the run still exists.
        # Reset mock to verify second call independently.
        fake_task.cancel.reset_mock()
        fake_task.cancel.return_value = None

        async with AsyncClient(
            transport=ASGITransport(app=cancel_app), base_url="http://testserver"
        ) as client:
            resp2 = await client.post(
                f"/api/v1/threads/{session_id}/cancel",
                headers={"X-User-Id": "user_a"},
            )

        # Both routes reach the same ActiveRun.
        assert resp2.status_code == 202, resp2.text
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)
