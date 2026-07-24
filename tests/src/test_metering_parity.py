"""Regression tests for metering parity (agent-general-chat G6).

Technical-design §4 states that `_run_agent_turn` is kind-agnostic and
DM/lookup-tool turns ride the existing metering pipeline without bypass:
  _run_agent_turn → check_quota (pre-turn) → emit_turn_cost (post-turn)

Test plan:
  1. DM turn (feature_id='') invokes check_quota with the same call shape as a
     feature-thread turn (feature_id='feat-1') — no kind-specific bypass.
  2. DM turn (feature_id='') emits turn cost identically to a feature-thread turn.
  3. A turn in a lookup-capable session (feature_id='', workflow_lookup_feature
     available) still calls check_quota — no discount path for read-only turns.
  4. Structural: check_quota is called only from _run_agent_turn; no new parallel
     cost-accounting entry point was introduced by the DM or lookup-tool work.

Implementation note (same as test_quota_cost.py):
  _run_agent_turn is a blocking function that uses asyncio.run_coroutine_threadsafe
  internally. Tests must run it via asyncio.to_thread so that
  run_coroutine_threadsafe can schedule work on the running event loop.
"""

from __future__ import annotations

import asyncio
import inspect
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
# Stub helpers (mirrors test_quota_cost._inject_stubs)
# ---------------------------------------------------------------------------


def _inject_stubs() -> None:
    """Inject minimal stubs so agent_dispatch imports cleanly without full stack."""
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

    plugins_stub = sys.modules.get("plugins")
    if plugins_stub is None:
        plugins_stub = types.ModuleType("plugins")
        sys.modules["plugins"] = plugins_stub
    if not getattr(plugins_stub, "__path__", None):
        # Other test modules' stub helpers sometimes leave a bare, path-less
        # "plugins" module cached in sys.modules. Repair it here so
        # `plugins.tools.guardrails` etc. (needed by src.api.scope_guard)
        # import for real via normal package machinery, without running the
        # heavy plugins/__init__.py import chain.
        plugins_stub.__path__ = [str(REPO_ROOT / "plugins")]

    for _mod in ("plugins.context", "plugins.skills"):
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


def _make_fake_agent(input_tokens: int = 300, output_tokens: int = 75) -> MagicMock:
    agent = MagicMock()
    agent.session_input_tokens = input_tokens
    agent.session_output_tokens = output_tokens
    agent.session_cache_read_tokens = 0
    agent.session_cache_write_tokens = 0
    agent.run_conversation = MagicMock(return_value={"final_response": "reply"})
    return agent


# ---------------------------------------------------------------------------
# Helpers for capturing quota and cost calls
# ---------------------------------------------------------------------------


def _make_allow_quota():
    async def _allow(sid, uid, **kw):
        from src.services.cost_client import QuotaCheckResult

        return QuotaCheckResult.fail_open()

    return _allow


def _make_capture_check_quota(records: list):
    async def _capture(sid, uid, **kw):
        from src.services.cost_client import QuotaCheckResult

        records.append({"session_id": sid, "user_id": uid, "org_id": kw.get("org_id")})
        return QuotaCheckResult.fail_open()

    return _capture


def _make_capture_emit_cost(records: list):
    async def _capture(sid, uid, model, **kw):
        records.append(
            {
                "session_id": sid,
                "user_id": uid,
                "model": model,
                "input_tokens": kw.get("input_tokens"),
                "output_tokens": kw.get("output_tokens"),
                "stopped": kw.get("stopped", False),
                "source_label": kw.get("source_label"),
            }
        )

    return _capture


async def _run_turn(
    *,
    session_id: str,
    feature_id: str,
    user_id: str = "user-test",
    workspace_id: str = "ws-test",
    check_quota_fn=None,
    emit_cost_fn=None,
    fake_agent=None,
) -> None:
    """Helper: run _run_agent_turn via asyncio.to_thread with injected stubs."""
    _inject_stubs()

    if fake_agent is None:
        fake_agent = _make_fake_agent()

    import run_agent as _stub

    _stub.AIAgent = MagicMock(return_value=fake_agent)

    from src.api import agent_dispatch

    loop = asyncio.get_event_loop()
    translator = MagicMock()
    translator.full_text = "reply"
    translator.on_delta = MagicMock()
    translator.done = MagicMock()

    ctx = {
        "run_id": f"run-{session_id}",
        "session_id": session_id,
        "message": "@agent help",
        "history": [],
        "workspace_id": workspace_id,
        "feature_id": feature_id,
        "user_id": user_id,
        "model": "test-model",
        "provider": None,
        "api_key": None,
        "base_url": None,
        "db_factory": _db_factory,
        "loop": loop,
        "translator": translator,
    }

    patches: list = []
    if check_quota_fn is not None:
        patches.append(patch.object(agent_dispatch, "check_quota", check_quota_fn))
    if emit_cost_fn is not None:
        patches.append(patch.object(agent_dispatch, "emit_turn_cost", emit_cost_fn))

    if patches:
        # Apply all patches
        import contextlib

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            await asyncio.to_thread(agent_dispatch._run_agent_turn, **ctx)
    else:
        await asyncio.to_thread(agent_dispatch._run_agent_turn, **ctx)


# ---------------------------------------------------------------------------
# Test 1 — DM turn calls check_quota (same call shape as feature-thread turn)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_turn_calls_check_quota():
    """_run_agent_turn for a DM session (feature_id='') calls check_quota."""
    _inject_stubs()

    quota_calls: list = []

    import run_agent as _stub

    _stub.AIAgent = MagicMock(return_value=_make_fake_agent())

    from src.api import agent_dispatch

    loop = asyncio.get_event_loop()
    translator = MagicMock()
    translator.full_text = ""
    translator.on_delta = MagicMock()
    translator.done = MagicMock()

    with (
        patch.object(agent_dispatch, "check_quota", _make_capture_check_quota(quota_calls)),
        patch.object(agent_dispatch, "emit_turn_cost", AsyncMock()),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-dm-1",
            session_id="sess_dm_001",
            message="@agent hello",
            history=[],
            workspace_id="ws-1",
            feature_id="",  # DM session: no feature_id
            user_id="user-dm",
            model="test-model",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=_db_factory,
            loop=loop,
            translator=translator,
        )

    assert len(quota_calls) == 1, (
        "check_quota must be called exactly once for a DM session turn"
    )
    assert quota_calls[0]["session_id"] == "sess_dm_001"
    assert quota_calls[0]["user_id"] == "user-dm"


@pytest.mark.asyncio
async def test_feature_thread_turn_calls_check_quota():
    """_run_agent_turn for a feature-thread (feature_id set) calls check_quota."""
    _inject_stubs()

    quota_calls: list = []

    import run_agent as _stub

    _stub.AIAgent = MagicMock(return_value=_make_fake_agent())

    from src.api import agent_dispatch

    loop = asyncio.get_event_loop()
    translator = MagicMock()
    translator.full_text = ""
    translator.on_delta = MagicMock()
    translator.done = MagicMock()

    with (
        patch.object(agent_dispatch, "check_quota", _make_capture_check_quota(quota_calls)),
        patch.object(agent_dispatch, "emit_turn_cost", AsyncMock()),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-ft-1",
            session_id="sess_ft_001",
            message="@agent help",
            history=[],
            workspace_id="ws-1",
            feature_id="agent-general-chat",  # feature-thread: feature_id is set
            user_id="user-ft",
            model="test-model",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=_db_factory,
            loop=loop,
            translator=translator,
        )

    assert len(quota_calls) == 1, (
        "check_quota must be called exactly once for a feature-thread turn"
    )
    assert quota_calls[0]["session_id"] == "sess_ft_001"
    assert quota_calls[0]["user_id"] == "user-ft"


@pytest.mark.asyncio
async def test_dm_and_feature_thread_check_quota_same_call_shape():
    """DM turn and feature-thread turn call check_quota with the same argument shape.

    This is the core metering-parity assertion: check_quota takes (session_id,
    user_id) regardless of the session kind. Neither path gets a bypass or a
    discount — both pass through the identical pre-turn gate.
    """
    _inject_stubs()

    dm_quota_calls: list = []
    ft_quota_calls: list = []

    import run_agent as _stub

    from src.api import agent_dispatch

    loop = asyncio.get_event_loop()

    def _make_translator():
        t = MagicMock()
        t.full_text = ""
        t.on_delta = MagicMock()
        t.done = MagicMock()
        return t

    base_kwargs = {
        "message": "@agent help",
        "history": [],
        "workspace_id": "ws-parity",
        "model": "test-model",
        "provider": None,
        "api_key": None,
        "base_url": None,
        "db_factory": _db_factory,
        "loop": loop,
    }

    _stub.AIAgent = MagicMock(return_value=_make_fake_agent())
    with (
        patch.object(agent_dispatch, "check_quota", _make_capture_check_quota(dm_quota_calls)),
        patch.object(agent_dispatch, "emit_turn_cost", AsyncMock()),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-dm-parity",
            session_id="sess_dm_parity",
            feature_id="",  # DM: no feature scope
            user_id="user-parity",
            translator=_make_translator(),
            **base_kwargs,
        )

    _stub.AIAgent = MagicMock(return_value=_make_fake_agent())
    with (
        patch.object(agent_dispatch, "check_quota", _make_capture_check_quota(ft_quota_calls)),
        patch.object(agent_dispatch, "emit_turn_cost", AsyncMock()),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-ft-parity",
            session_id="sess_ft_parity",
            feature_id="some-feature",  # feature-thread
            user_id="user-parity",
            translator=_make_translator(),
            **base_kwargs,
        )

    # Both turns must call check_quota exactly once.
    assert len(dm_quota_calls) == 1, "DM turn must call check_quota"
    assert len(ft_quota_calls) == 1, "Feature-thread turn must call check_quota"

    # Call shape is identical: positional (session_id, user_id) — no kind-specific args.
    dm_call = dm_quota_calls[0]
    ft_call = ft_quota_calls[0]
    assert set(dm_call.keys()) == set(ft_call.keys()), (
        "check_quota call shape must be identical for DM and feature-thread turns; "
        f"DM keys={set(dm_call.keys())}, FT keys={set(ft_call.keys())}"
    )


# ---------------------------------------------------------------------------
# Test 2 — DM turn emits cost event identically to a feature-thread turn
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dm_turn_emits_turn_cost():
    """_run_agent_turn for a DM session emits a post-turn cost event."""
    _inject_stubs()

    cost_events: list = []
    fake_agent = _make_fake_agent(input_tokens=400, output_tokens=100)

    import run_agent as _stub

    _stub.AIAgent = MagicMock(return_value=fake_agent)

    from src.api import agent_dispatch

    loop = asyncio.get_event_loop()
    translator = MagicMock()
    translator.full_text = "ok"
    translator.on_delta = MagicMock()
    translator.done = MagicMock()

    with (
        patch.object(agent_dispatch, "check_quota", _make_allow_quota()),
        patch.object(agent_dispatch, "emit_turn_cost", _make_capture_emit_cost(cost_events)),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-dm-cost",
            session_id="sess_dm_cost",
            message="@agent help",
            history=[],
            workspace_id="ws-1",
            feature_id="",  # DM session
            user_id="user-cost",
            model="claude-sonnet-4-6",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=_db_factory,
            loop=loop,
            translator=translator,
        )

    assert cost_events, "DM session turn must emit a post-turn cost event"
    evt = cost_events[0]
    assert evt["session_id"] == "sess_dm_cost"
    assert evt["user_id"] == "user-cost"
    assert evt["input_tokens"] == 400
    assert evt["output_tokens"] == 100
    assert evt["stopped"] is False


@pytest.mark.asyncio
async def test_dm_and_feature_thread_emit_cost_with_same_shape():
    """DM and feature-thread turns emit cost events with the same argument shape.

    Neither session kind receives a discount, bypass, or reduced-frequency
    emission — the post-turn emit_turn_cost call is structurally identical.
    """
    _inject_stubs()

    dm_costs: list = []
    ft_costs: list = []

    import run_agent as _stub

    from src.api import agent_dispatch

    loop = asyncio.get_event_loop()

    def _make_translator():
        t = MagicMock()
        t.full_text = "done"
        t.on_delta = MagicMock()
        t.done = MagicMock()
        return t

    base_kwargs = {
        "message": "@agent help",
        "history": [],
        "workspace_id": "ws-cost-parity",
        "model": "claude-sonnet-4-6",
        "provider": None,
        "api_key": None,
        "base_url": None,
        "db_factory": _db_factory,
        "loop": loop,
    }

    _stub.AIAgent = MagicMock(return_value=_make_fake_agent(200, 50))
    with (
        patch.object(agent_dispatch, "check_quota", _make_allow_quota()),
        patch.object(agent_dispatch, "emit_turn_cost", _make_capture_emit_cost(dm_costs)),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-dm-shape",
            session_id="sess_dm_shape",
            feature_id="",  # DM
            user_id="user-shape",
            translator=_make_translator(),
            **base_kwargs,
        )

    _stub.AIAgent = MagicMock(return_value=_make_fake_agent(200, 50))
    with (
        patch.object(agent_dispatch, "check_quota", _make_allow_quota()),
        patch.object(agent_dispatch, "emit_turn_cost", _make_capture_emit_cost(ft_costs)),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-ft-shape",
            session_id="sess_ft_shape",
            feature_id="some-feature",  # feature-thread
            user_id="user-shape",
            translator=_make_translator(),
            **base_kwargs,
        )

    assert dm_costs, "DM turn must emit cost"
    assert ft_costs, "Feature-thread turn must emit cost"

    dm_evt = dm_costs[0]
    ft_evt = ft_costs[0]

    # Both events carry the same fields — no kind-specific stripping or addition.
    assert set(dm_evt.keys()) == set(ft_evt.keys()), (
        "emit_turn_cost event keys must be identical for DM and feature-thread; "
        f"DM={set(dm_evt.keys())}, FT={set(ft_evt.keys())}"
    )
    # Token counts reflect the agent — not zeroed out for DM.
    assert dm_evt["input_tokens"] > 0
    assert dm_evt["output_tokens"] > 0
    assert dm_evt["stopped"] is False


# ---------------------------------------------------------------------------
# Test 3 — Lookup-tool session hits check_quota (no bypass for read-only turns)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lookup_tool_session_calls_check_quota():
    """A session with feature_id='' (where workflow_lookup_feature is available)
    still calls check_quota — no quota bypass for read-only/lookup-only turns.

    The lookup tool is surfaced to the agent purely as a choice at inference time;
    it does not create a separate dispatch path that bypasses the metering gate.
    """
    _inject_stubs()

    quota_calls: list = []
    fake_agent = _make_fake_agent()

    import run_agent as _stub

    _stub.AIAgent = MagicMock(return_value=fake_agent)

    from src.api import agent_dispatch

    loop = asyncio.get_event_loop()
    translator = MagicMock()
    translator.full_text = ""
    translator.on_delta = MagicMock()
    translator.done = MagicMock()

    # feature_id='' → this is a Channel/DM/general session where
    # workflow_lookup_feature would be available (check_available returns True).
    with (
        patch.object(agent_dispatch, "check_quota", _make_capture_check_quota(quota_calls)),
        patch.object(agent_dispatch, "emit_turn_cost", AsyncMock()),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-lookup-1",
            session_id="sess_lookup_001",
            message="@agent which feature handles auth?",
            history=[],
            workspace_id="ws-lookup",
            feature_id="",  # lookup-capable session (no feature scope)
            user_id="user-lookup",
            model="test-model",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=_db_factory,
            loop=loop,
            translator=translator,
        )

    assert len(quota_calls) == 1, (
        "check_quota must fire even when the session is a lookup-capable "
        "general/DM session (no quota bypass for read-only tool turns)"
    )
    assert quota_calls[0]["session_id"] == "sess_lookup_001"
    assert quota_calls[0]["user_id"] == "user-lookup"


@pytest.mark.asyncio
async def test_lookup_tool_session_emits_turn_cost():
    """A lookup-tool session (feature_id='') still emits a post-turn cost event.

    workflow_lookup_feature is a read-only tool but its use does not discount or
    suppress the post-turn cost emission — the agent ran and consumed tokens.
    """
    _inject_stubs()

    cost_events: list = []
    fake_agent = _make_fake_agent(input_tokens=250, output_tokens=60)

    import run_agent as _stub

    _stub.AIAgent = MagicMock(return_value=fake_agent)

    from src.api import agent_dispatch

    loop = asyncio.get_event_loop()
    translator = MagicMock()
    translator.full_text = "The auth feature is called user-auth."
    translator.on_delta = MagicMock()
    translator.done = MagicMock()

    with (
        patch.object(agent_dispatch, "check_quota", _make_allow_quota()),
        patch.object(agent_dispatch, "emit_turn_cost", _make_capture_emit_cost(cost_events)),
    ):
        await asyncio.to_thread(
            agent_dispatch._run_agent_turn,
            run_id="run-lookup-cost",
            session_id="sess_lookup_cost",
            message="@agent which feature handles auth?",
            history=[],
            workspace_id="ws-lookup",
            feature_id="",  # lookup-capable session
            user_id="user-lookup",
            model="claude-sonnet-4-6",
            provider=None,
            api_key=None,
            base_url=None,
            db_factory=_db_factory,
            loop=loop,
            translator=translator,
        )

    assert cost_events, "Lookup-tool session must emit a post-turn cost event"
    evt = cost_events[0]
    assert evt["input_tokens"] == 250
    assert evt["output_tokens"] == 60
    assert evt["stopped"] is False


# ---------------------------------------------------------------------------
# Test 4 — Structural: _run_agent_turn is the single cost-accounting entry point
# ---------------------------------------------------------------------------


def test_check_quota_called_only_from_run_agent_turn():
    """check_quota is called only from _run_agent_turn and _run_opencode_turn.

    _run_agent_turn      → the Hermes AIAgent path's pre-turn quota gate.
    _run_opencode_turn   → the opencode-backed coding-verdict path for an
                           IDE-originated /chat turn (see _run_agent_turn's
                           coding-triage branch); it bypasses AIAgent
                           entirely so it needs its own gate, not a bypass.

    No other new parallel function may dispatch a turn while bypassing the
    pre-turn quota gate.

    We inspect the agent_dispatch module source and verify that every call site
    of check_quota is within one of these two function bodies.
    """
    # Import without triggering the full stub setup — just need source analysis.
    import ast

    from src.api import agent_dispatch

    source = inspect.getsource(agent_dispatch)
    tree = ast.parse(source)

    # Collect all function definitions (top-level and nested).
    funcs_calling_check_quota: list[str] = []
    funcs_calling_emit_turn_cost: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        func_source = ast.get_source_segment(source, node) or ""
        if "check_quota" in func_source:
            funcs_calling_check_quota.append(node.name)
        if "emit_turn_cost" in func_source:
            funcs_calling_emit_turn_cost.append(node.name)

    # check_quota must only appear inside these two known turn-gates.
    expected = {"_run_agent_turn", "_run_opencode_turn"}
    actual = set(funcs_calling_check_quota)
    unexpected = actual - expected
    assert not unexpected, (
        f"check_quota must only be called from {expected}. "
        f"Unexpected callers found: {unexpected}. "
        "A new caller may bypass the pre-turn quota gate."
    )


def test_emit_turn_cost_called_only_from_expected_functions():
    """emit_turn_cost is called only from these three known functions.

    _run_agent_turn       → post-turn cost emission (successful/failed turns)
    _run_agent_turn_async → stopped-turn cost emission (CancelledError path)
    _run_opencode_turn    → post-turn cost emission for an opencode-backed
                            coding-verdict turn (bypasses AIAgent entirely,
                            so it needs its own emission, not a bypass)

    No other new function was introduced that accounts for cost separately
    (which would create a second accounting path violating G6).
    """
    import ast

    from src.api import agent_dispatch

    source = inspect.getsource(agent_dispatch)
    tree = ast.parse(source)

    funcs_calling_emit: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        func_source = ast.get_source_segment(source, node) or ""
        if "emit_turn_cost" in func_source:
            funcs_calling_emit.append(node.name)

    expected = {"_run_agent_turn", "_run_agent_turn_async", "_run_opencode_turn"}
    actual = set(funcs_calling_emit)
    unexpected = actual - expected
    assert not unexpected, (
        f"emit_turn_cost must only be called from {expected}. "
        f"Unexpected callers found: {unexpected}. "
        "A third caller would create a parallel cost-accounting path (G6 violation)."
    )


def test_run_agent_turn_remains_blocking_entry_point():
    """_run_agent_turn is a plain (non-async) function — it is the blocking entry
    point called by _run_agent_turn_async via loop.run_in_executor.

    If _run_agent_turn were made async it would no longer be the blocking worker
    that asyncio.run_coroutine_threadsafe can call from a thread pool, breaking
    the turn lifecycle.  This structural test guards against accidental
    async-ification.
    """
    import ast

    from src.api import agent_dispatch

    source = inspect.getsource(agent_dispatch)
    tree = ast.parse(source)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_run_agent_turn":
            return  # found as a sync def — correct

    # If we reach here, _run_agent_turn is either missing or async.
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_run_agent_turn":
            pytest.fail(
                "_run_agent_turn must be a synchronous (blocking) function "
                "so that run_in_executor + asyncio.run_coroutine_threadsafe work "
                "correctly. It must not be async."
            )
    pytest.fail("_run_agent_turn not found in agent_dispatch module.")
