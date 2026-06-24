"""Tests for m4-agent-cost T5: pre-turn quota guard + post-turn cost emission + stopped-turn tally.

Test plan (from tasks.md):
  - Blocked turn (daily quota) spends zero tokens + posts system message + composer stays enabled
  - Blocked turn (weekly quota) same as above with weekly reason
  - Successful turn emits a cost event with stopped=False after completion
  - Stopped turn emits cost event with stopped=True and partial token counts (>0, <=full turn)
  - Quota check fail-open (BFF unreachable) allows turn to proceed
  - BFF client check_quota and emit_turn_cost: skipped when WORKFLOW_BFF_URL unset

Implementation note:
  _run_agent_turn() is designed to run on a worker thread while the event loop is active
  (it uses asyncio.run_coroutine_threadsafe). Tests must run it via asyncio.to_thread so
  that run_coroutine_threadsafe can schedule work on the running loop.
"""

from __future__ import annotations

import asyncio
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Stub helpers
# ---------------------------------------------------------------------------


def _inject_stubs():
    """Inject minimal stubs so agent_dispatch imports cleanly."""
    if "run_agent" not in sys.modules:
        stub = types.ModuleType("run_agent")
        stub.AIAgent = MagicMock()
        sys.modules["run_agent"] = stub

    if "hermes_state" not in sys.modules:
        stub = types.ModuleType("hermes_state")

        class _FakeSessionDB:
            def append_message(self, *a, **kw):
                return 0

            def update_token_counts(self, *a, **kw):
                pass

        stub.SessionDB = _FakeSessionDB
        sys.modules["hermes_state"] = stub

    for _mod in ("plugins", "plugins.context", "plugins.skills"):
        if _mod not in sys.modules:
            sys.modules[_mod] = types.ModuleType(_mod)

    ctx = sys.modules.get("plugins.context") or types.ModuleType("plugins.context")
    if not hasattr(ctx, "set_context"):
        ctx.set_context = MagicMock()
        ctx.clear_context = MagicMock()
        sys.modules["plugins.context"] = ctx
    if not hasattr(ctx, "set_agent_context"):
        ctx.set_agent_context = MagicMock()

    skills = sys.modules.get("plugins.skills") or types.ModuleType("plugins.skills")
    if not hasattr(skills, "get_shared_rules"):
        skills.get_shared_rules = lambda: None
        sys.modules["plugins.skills"] = skills


@asynccontextmanager
async def _db_factory():
    yield MagicMock()


# ---------------------------------------------------------------------------
# BFF client unit tests (no HTTP)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_check_quota_skipped_when_url_unset(monkeypatch):
    """check_quota fails open (allowed=True) when WORKFLOW_BFF_URL is unset."""
    monkeypatch.delenv("WORKFLOW_BFF_URL", raising=False)
    from src.services.bff_client import check_quota

    result = await check_quota("session-1", "user-1")
    assert result.allowed is True


@pytest.mark.asyncio
async def test_emit_turn_cost_skipped_when_url_unset(monkeypatch):
    """emit_turn_cost is a no-op when WORKFLOW_BFF_URL is unset."""
    monkeypatch.delenv("WORKFLOW_BFF_URL", raising=False)
    from src.services.bff_client import emit_turn_cost

    # Should not raise even with zero tokens.
    await emit_turn_cost(
        "session-1",
        "user-1",
        "claude-sonnet-4-6",
        input_tokens=0,
        output_tokens=0,
    )


@pytest.mark.asyncio
async def test_check_quota_returns_blocked_on_200(monkeypatch):
    """check_quota returns the BFF's allowed/reason/resets_at on a 200 response."""
    monkeypatch.setenv("WORKFLOW_BFF_URL", "http://bff:8080")
    from src.services.bff_client import check_quota

    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.json = AsyncMock(
        return_value={
            "allowed": False,
            "reason": "daily_exceeded",
            "resets_at": "2026-06-25T00:00:00Z",
            "plan_name": "free",
            "daily_cap": 10000,
            "weekly_cap": 50000,
        }
    )
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=False)

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.get = MagicMock(return_value=fake_resp)

    with patch("src.services.bff_client.aiohttp.ClientSession", return_value=fake_session):
        result = await check_quota("session-1", "user-1")

    assert result.allowed is False
    assert result.reason == "daily_exceeded"
    assert result.resets_at == "2026-06-25T00:00:00Z"
    assert result.daily_cap == 10000


@pytest.mark.asyncio
async def test_check_quota_fails_open_on_network_error(monkeypatch):
    """check_quota returns allowed=True when the BFF call raises an exception."""
    monkeypatch.setenv("WORKFLOW_BFF_URL", "http://bff:8080")
    from src.services.bff_client import check_quota

    with patch("src.services.bff_client.aiohttp.ClientSession", side_effect=OSError("refused")):
        result = await check_quota("session-1", "user-1")

    assert result.allowed is True


@pytest.mark.asyncio
async def test_emit_turn_cost_posts_payload(monkeypatch):
    """emit_turn_cost POSTs the expected payload to the BFF."""
    monkeypatch.setenv("WORKFLOW_BFF_URL", "http://bff:8080")
    from src.services.bff_client import emit_turn_cost

    posted: Dict[str, Any] = {}

    fake_resp = MagicMock()
    fake_resp.status = 201
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=False)

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    def _fake_post(url, json=None, headers=None, timeout=None):
        posted.update(json or {})
        posted["_url"] = url
        return fake_resp

    fake_session.post = _fake_post

    with patch("src.services.bff_client.aiohttp.ClientSession", return_value=fake_session):
        await emit_turn_cost(
            "session-42",
            "user-7",
            "claude-sonnet-4-6",
            input_tokens=1000,
            output_tokens=200,
            cache_read_tokens=50,
            cache_write_tokens=10,
            stopped=False,
        )

    assert posted["user_id"] == "user-7"
    assert posted["model"] == "claude-sonnet-4-6"
    assert posted["input_tokens"] == 1000
    assert posted["output_tokens"] == 200
    assert posted["cache_read_tokens"] == 50
    assert posted["cache_write_tokens"] == 10
    assert posted["stopped"] is False
    assert "session-42" in posted["_url"]


@pytest.mark.asyncio
async def test_emit_turn_cost_sets_stopped_true_for_partial(monkeypatch):
    """emit_turn_cost sets stopped=True when the stopped flag is passed."""
    monkeypatch.setenv("WORKFLOW_BFF_URL", "http://bff:8080")
    from src.services.bff_client import emit_turn_cost

    posted: Dict[str, Any] = {}

    fake_resp = MagicMock()
    fake_resp.status = 201
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=False)

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)

    def _fake_post(url, json=None, headers=None, timeout=None):
        posted.update(json or {})
        return fake_resp

    fake_session.post = _fake_post

    with patch("src.services.bff_client.aiohttp.ClientSession", return_value=fake_session):
        await emit_turn_cost(
            "session-42",
            "user-7",
            "claude-sonnet-4-6",
            input_tokens=500,
            output_tokens=80,
            stopped=True,
        )

    assert posted["stopped"] is True
    assert posted["input_tokens"] == 500
    assert posted["output_tokens"] == 80


# ---------------------------------------------------------------------------
# Pre-turn quota guard tests — run _run_agent_turn via asyncio.to_thread
# so asyncio.run_coroutine_threadsafe works correctly.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_blocked_daily_posts_system_message_no_agent_constructed():
    """Daily quota exceeded: system message posted, zero tokens spent (no AIAgent)."""
    _inject_stubs()

    from src.services.bff_client import QuotaCheckResult

    quota = QuotaCheckResult(
        allowed=False,
        reason="daily_exceeded",
        resets_at="2026-06-25T00:00:00Z",
        plan_name="free",
        daily_cap=10000,
    )

    appended: list = []
    ai_agent_calls: list = []

    async def _fake_check_quota(sid, uid, **kw):
        return quota

    async def _fake_emit(*a, **kw):
        pass

    async def _fake_append(db, session_id, role="", content="", **kw):
        appended.append({"role": role, "content": content})
        return len(appended)

    loop = asyncio.get_event_loop()

    translator = MagicMock()
    translator.full_text = ""
    translator.on_delta = MagicMock()
    translator.done = MagicMock()
    translator.mark_stopped = MagicMock(return_value="")

    import run_agent as _run_agent_stub
    _run_agent_stub.AIAgent = MagicMock(
        side_effect=lambda **kw: ai_agent_calls.append(kw) or MagicMock()
    )

    from src.api import agent_dispatch

    with (
        patch.object(agent_dispatch, "check_quota", _fake_check_quota),
        patch.object(agent_dispatch, "emit_turn_cost", _fake_emit),
        patch("src.db.store.append_message", _fake_append),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-quota-daily",
            session_id="sess-quota-daily",
            message="@agent help",
            history=[],
            workspace_id="ws-1",
            feature_id="feat-1",
            user_id="user-q",
            model="test-model",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=_db_factory,
            loop=loop,
            translator=translator,
        )

    # No AIAgent constructed → zero tokens spent.
    assert len(ai_agent_calls) == 0, "AIAgent must NOT be constructed on quota block"

    # A system message was posted.
    system_msgs = [m for m in appended if m["role"] == "system"]
    assert system_msgs, f"Expected a system message, appended: {appended}"
    assert "daily" in system_msgs[0]["content"].lower()
    assert "10000" in system_msgs[0]["content"]

    # Composer stays enabled — done() called (turn completed cleanly, no error).
    translator.done.assert_called()


@pytest.mark.asyncio
async def test_quota_blocked_weekly_posts_system_message():
    """Weekly quota exceeded: system message contains 'weekly'."""
    _inject_stubs()

    from src.services.bff_client import QuotaCheckResult

    quota = QuotaCheckResult(
        allowed=False,
        reason="weekly_exceeded",
        resets_at="2026-06-29T00:00:00Z",
        plan_name="free",
        weekly_cap=50000,
    )

    appended: list = []
    ai_agent_calls: list = []

    async def _fake_check_quota(sid, uid, **kw):
        return quota

    async def _fake_emit(*a, **kw):
        pass

    async def _fake_append(db, session_id, role="", content="", **kw):
        appended.append({"role": role, "content": content})
        return len(appended)

    loop = asyncio.get_event_loop()

    translator = MagicMock()
    translator.full_text = ""
    translator.on_delta = MagicMock()
    translator.done = MagicMock()
    translator.mark_stopped = MagicMock(return_value="")

    import run_agent as _run_agent_stub
    _run_agent_stub.AIAgent = MagicMock(
        side_effect=lambda **kw: ai_agent_calls.append(kw) or MagicMock()
    )

    from src.api import agent_dispatch

    with (
        patch.object(agent_dispatch, "check_quota", _fake_check_quota),
        patch.object(agent_dispatch, "emit_turn_cost", _fake_emit),
        patch("src.db.store.append_message", _fake_append),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-quota-weekly",
            session_id="sess-quota-weekly",
            message="@agent help",
            history=[],
            workspace_id="ws-1",
            feature_id="feat-1",
            user_id="user-q",
            model="test-model",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=_db_factory,
            loop=loop,
            translator=translator,
        )

    assert len(ai_agent_calls) == 0, "AIAgent must NOT be constructed on weekly quota block"
    system_msgs = [m for m in appended if m["role"] == "system"]
    assert system_msgs, f"Expected a system message, appended: {appended}"
    assert "weekly" in system_msgs[0]["content"].lower()


@pytest.mark.asyncio
async def test_quota_fail_open_allows_turn():
    """When quota check raises, the turn proceeds (fail-open)."""
    _inject_stubs()

    ai_agent_calls: list = []

    async def _raising_check_quota(sid, uid, **kw):
        raise OSError("BFF unreachable")

    emitted_costs: list = []

    async def _fake_emit(*a, stopped=False, **kw):
        emitted_costs.append({"stopped": stopped})

    fake_agent = MagicMock()
    fake_agent.session_input_tokens = 100
    fake_agent.session_output_tokens = 50
    fake_agent.session_cache_read_tokens = 0
    fake_agent.session_cache_write_tokens = 0
    fake_agent.run_conversation = MagicMock(return_value={"final_response": "ok"})

    loop = asyncio.get_event_loop()

    translator = MagicMock()
    translator.full_text = ""
    translator.on_delta = MagicMock()
    translator.done = MagicMock()
    translator.mark_stopped = MagicMock(return_value="")

    import run_agent as _run_agent_stub
    _run_agent_stub.AIAgent = MagicMock(
        side_effect=lambda **kw: ai_agent_calls.append(kw) or fake_agent
    )

    from src.api import agent_dispatch

    with (
        patch.object(agent_dispatch, "check_quota", _raising_check_quota),
        patch.object(agent_dispatch, "emit_turn_cost", _fake_emit),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-failopen",
            session_id="sess-failopen",
            message="@agent help",
            history=[],
            workspace_id="ws-1",
            feature_id="feat-1",
            user_id="user-q",
            model="test-model",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=_db_factory,
            loop=loop,
            translator=translator,
        )

    # AIAgent was constructed → turn proceeded despite quota check failure.
    assert len(ai_agent_calls) == 1, "Turn must proceed on quota check failure (fail-open)"


# ---------------------------------------------------------------------------
# Post-turn cost emission (successful turn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_turn_emits_cost_event():
    """A completed turn emits a cost event with stopped=False and the agent's token counts."""
    _inject_stubs()

    emitted: list = []

    async def _allow_check_quota(sid, uid, **kw):
        from src.services.bff_client import QuotaCheckResult

        return QuotaCheckResult.fail_open()

    async def _capture_emit(sid, uid, model, *, input_tokens, output_tokens, stopped=False, **kw):
        emitted.append({
            "session_id": sid,
            "user_id": uid,
            "model": model,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "stopped": stopped,
        })

    fake_agent = MagicMock()
    fake_agent.session_input_tokens = 500
    fake_agent.session_output_tokens = 120
    fake_agent.session_cache_read_tokens = 30
    fake_agent.session_cache_write_tokens = 5
    fake_agent.run_conversation = MagicMock(return_value={"final_response": "done"})

    loop = asyncio.get_event_loop()

    translator = MagicMock()
    translator.full_text = "done"
    translator.on_delta = MagicMock()
    translator.done = MagicMock()
    translator.mark_stopped = MagicMock(return_value="")

    import run_agent as _run_agent_stub
    _run_agent_stub.AIAgent = MagicMock(return_value=fake_agent)

    from src.api import agent_dispatch

    with (
        patch.object(agent_dispatch, "check_quota", _allow_check_quota),
        patch.object(agent_dispatch, "emit_turn_cost", _capture_emit),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-success",
            session_id="sess-success",
            message="@agent help",
            history=[],
            workspace_id="ws-1",
            feature_id="feat-1",
            user_id="user-s",
            model="claude-sonnet-4-6",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=_db_factory,
            loop=loop,
            translator=translator,
        )

    assert emitted, "Expected a cost event to be emitted"
    evt = emitted[0]
    assert evt["stopped"] is False
    assert evt["input_tokens"] == 500
    assert evt["output_tokens"] == 120
    assert evt["session_id"] == "sess-success"


# ---------------------------------------------------------------------------
# Stopped-turn cost emission (Decision B1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopped_turn_emits_partial_cost_with_stopped_true():
    """CancelledError path emits a cost event with stopped=True and partial token counts > 0."""
    _inject_stubs()

    emitted: list = []

    async def _capture_emit(sid, uid, model, *, input_tokens, output_tokens, stopped=False, **kw):
        emitted.append({
            "stopped": stopped,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        })

    from src.api.agent_dispatch import _run_agent_turn_async, ActiveRun, _active_runs, _active_runs_lock
    from src.streaming.sse import HermesSSETranslator

    session_id = "sess-stopped"
    loop = asyncio.get_event_loop()

    translator = MagicMock(spec=HermesSSETranslator)
    translator.mark_stopped.return_value = "partial response"

    # Simulate an agent that accumulated tokens before interrupt.
    fake_agent = MagicMock()
    fake_agent.session_input_tokens = 300
    fake_agent.session_output_tokens = 75
    fake_agent.session_cache_read_tokens = 10
    fake_agent.session_cache_write_tokens = 2

    run_id = "run-stopped-cost"

    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(
            run_id=run_id, task=None, triggered_by="user-s"
        )
        _active_runs[session_id].agent = fake_agent

    async def _cancelled_executor(*args, **kwargs):
        raise asyncio.CancelledError()

    try:
        with (
            patch("src.api.agent_dispatch.get_bus") as mock_bus,
            patch.object(loop, "run_in_executor", side_effect=_cancelled_executor),
            patch("src.api.agent_dispatch.emit_turn_cost", _capture_emit),
        ):
            mock_bus.return_value = MagicMock(publish=MagicMock())
            await _run_agent_turn_async(
                run_id=run_id,
                session_id=session_id,
                triggered_by="user-s",
                message="@agent help",
                history=[],
                workspace_id="ws-1",
                feature_id="feat-1",
                user_id="user-s",
                model="claude-sonnet-4-6",
                provider=None,
                api_key=None,
                base_url=None,
                db_factory=_db_factory,
                loop=loop,
                translator=translator,
            )
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)

    assert emitted, "Expected a stopped-turn cost event"
    evt = emitted[0]
    assert evt["stopped"] is True, f"Expected stopped=True, got: {evt}"
    # Partial token counts from the agent's accumulated session counters.
    assert evt["input_tokens"] == 300
    assert evt["output_tokens"] == 75


@pytest.mark.asyncio
async def test_stopped_turn_without_agent_emits_zero_tokens():
    """If cancel races before agent construction, emit zero tokens with stopped=True."""
    _inject_stubs()

    emitted: list = []

    async def _capture_emit(sid, uid, model, *, input_tokens, output_tokens, stopped=False, **kw):
        emitted.append({
            "stopped": stopped,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        })

    from src.api.agent_dispatch import _run_agent_turn_async, ActiveRun, _active_runs, _active_runs_lock
    from src.streaming.sse import HermesSSETranslator

    session_id = "sess-stopped-noagent"
    loop = asyncio.get_event_loop()

    translator = MagicMock(spec=HermesSSETranslator)
    translator.mark_stopped.return_value = ""

    run_id = "run-stopped-noagent"

    with _active_runs_lock:
        # No agent attached yet.
        _active_runs[session_id] = ActiveRun(
            run_id=run_id, task=None, triggered_by="user-s"
        )

    async def _cancelled_executor(*args, **kwargs):
        raise asyncio.CancelledError()

    try:
        with (
            patch("src.api.agent_dispatch.get_bus") as mock_bus,
            patch.object(loop, "run_in_executor", side_effect=_cancelled_executor),
            patch("src.api.agent_dispatch.emit_turn_cost", _capture_emit),
        ):
            mock_bus.return_value = MagicMock(publish=MagicMock())
            await _run_agent_turn_async(
                run_id=run_id,
                session_id=session_id,
                triggered_by="user-s",
                message="@agent help",
                history=[],
                workspace_id="ws-1",
                feature_id="feat-1",
                user_id="user-s",
                model="claude-sonnet-4-6",
                provider=None,
                api_key=None,
                base_url=None,
                db_factory=_db_factory,
                loop=loop,
                translator=translator,
            )
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)

    assert emitted, "Expected a stopped cost event even with zero tokens"
    assert emitted[0]["stopped"] is True
    assert emitted[0]["input_tokens"] == 0
    assert emitted[0]["output_tokens"] == 0


@pytest.mark.asyncio
async def test_stopped_turn_token_count_at_most_comparable_full_turn():
    """Stopped-turn token counts are non-negative and ≤ a comparable full turn's counts."""
    _inject_stubs()

    # Full turn: agent accumulated 1000 input, 250 output.
    full_input = 1000
    full_output = 250

    stopped_emitted: list = []
    full_emitted: list = []

    async def _capture_stopped_emit(sid, uid, model, *, input_tokens, output_tokens, stopped=False, **kw):
        if stopped:
            stopped_emitted.append({"input_tokens": input_tokens, "output_tokens": output_tokens})
        else:
            full_emitted.append({"input_tokens": input_tokens, "output_tokens": output_tokens})

    from src.api.agent_dispatch import _run_agent_turn_async, ActiveRun, _active_runs, _active_runs_lock
    from src.streaming.sse import HermesSSETranslator

    session_id = "sess-compare"
    loop = asyncio.get_event_loop()

    translator = MagicMock(spec=HermesSSETranslator)
    translator.mark_stopped.return_value = "partial"

    # Simulate partial: agent had 600 input, 80 output when stopped (subset of full turn).
    fake_agent = MagicMock()
    fake_agent.session_input_tokens = 600
    fake_agent.session_output_tokens = 80
    fake_agent.session_cache_read_tokens = 0
    fake_agent.session_cache_write_tokens = 0

    run_id = "run-compare"

    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(run_id=run_id, task=None, triggered_by="user-s")
        _active_runs[session_id].agent = fake_agent

    async def _cancelled_executor(*args, **kwargs):
        raise asyncio.CancelledError()

    try:
        with (
            patch("src.api.agent_dispatch.get_bus") as mock_bus,
            patch.object(loop, "run_in_executor", side_effect=_cancelled_executor),
            patch("src.api.agent_dispatch.emit_turn_cost", _capture_stopped_emit),
        ):
            mock_bus.return_value = MagicMock(publish=MagicMock())
            await _run_agent_turn_async(
                run_id=run_id,
                session_id=session_id,
                triggered_by="user-s",
                message="@agent help",
                history=[],
                workspace_id="ws-1",
                feature_id="feat-1",
                user_id="user-s",
                model="claude-sonnet-4-6",
                provider=None,
                api_key=None,
                base_url=None,
                db_factory=_db_factory,
                loop=loop,
                translator=translator,
            )
    finally:
        with _active_runs_lock:
            _active_runs.pop(session_id, None)

    assert stopped_emitted, "Expected stopped cost event"
    partial = stopped_emitted[0]
    # Partial is non-negative.
    assert partial["input_tokens"] >= 0
    assert partial["output_tokens"] >= 0
    # Partial ≤ comparable full turn counts.
    assert partial["input_tokens"] <= full_input
    assert partial["output_tokens"] <= full_output
