"""Shared agent-dispatch state and worker-thread turn executor.

Centralises:
  - _active_runs / _active_runs_lock  (in-flight session guard, shared with legacy /chat)
  - _pending_agent_turns / _pending_lock  (coalescing pending follow-up turns)
  - _run_agent_turn()  (blocking worker-thread function)
  - schedule_agent_turn()  (async entry point for the send service)
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

import re as _re

from src.realtime.bus import get_bus
from src.services.cost_client import QuotaCheckResult, check_quota, emit_turn_cost
from src.streaming import BusPublishingSSETranslator, HermesSSETranslator

logger = logging.getLogger(__name__)

_WORD_RE = _re.compile(r"\S+\s*|\s+")

# Greetings / affirmations / very short messages never need extended thinking.
# Skipping reasoning on these is the single biggest win for time-to-reply on a
# "hi": thinking adds ~3-5s, and it's pure waste when the model only has to say
# hello. Substantive questions don't match this and still get the full trace.
_TRIVIAL_MSG_RE = _re.compile(
    r"^(hi|hey|hello|yo|sup|gm|hiya|howdy|good\s*(morning|afternoon|evening)|"
    r"thanks|thank\s*you|ty|thx|ok|okay|k|kk|yes|yep|yeah|yup|sure|"
    r"go\s*ahead|please\s*do|do\s*it|continue|proceed|sounds\s*good|"
    r"got\s*it|cool|nice|great|perfect|awesome|no|nope)"
    r"[\s!.?,…]*$",
    _re.IGNORECASE,
)


def _is_trivial_message(text: str) -> bool:
    """True for greetings/affirmations/very short messages — skip thinking on these."""
    t = (text or "").strip()
    return len(t) <= 2 or bool(_TRIVIAL_MSG_RE.match(t))


def _make_delta_callback(cb: Callable) -> Callable:
    """Wrap a stream-delta callback to split large chunks into word-sized pieces.

    Controlled by HERMES_STREAM_CHUNK_CHARS (default 0 = word-split enabled).
    Set to a positive integer to use fixed-size chunks, or -1 to disable
    splitting entirely (raw model chunks, may be 50-100 chars each).

    Example .env settings:
        HERMES_STREAM_CHUNK_CHARS=0   # word-by-word split (default)
        HERMES_STREAM_CHUNK_CHARS=4   # fixed 4-char chunks
        HERMES_STREAM_CHUNK_CHARS=-1  # no splitting
    """
    chunk_chars = int(os.environ.get("HERMES_STREAM_CHUNK_CHARS", "0"))

    if chunk_chars < 0:
        # Splitting disabled — pass through raw.
        return cb

    if chunk_chars == 0:
        # Word-by-word split.
        def _word_cb(delta: Any = None, **kwargs: Any) -> None:
            if not delta:
                return
            for part in _WORD_RE.findall(str(delta)) or [str(delta)]:
                cb(part, **kwargs)

        return _word_cb

    # Fixed-size chunks.
    def _fixed_cb(delta: Any = None, **kwargs: Any) -> None:
        if not delta:
            return
        text = str(delta)
        for i in range(0, len(text), chunk_chars):
            cb(text[i : i + chunk_chars], **kwargs)

    return _fixed_cb


# ---------------------------------------------------------------------------
# Shared in-flight guard (also used by the legacy /chat route)
# ---------------------------------------------------------------------------


@dataclass
class ActiveRun:
    """Tracks a live agent turn for the cancellation endpoint."""

    run_id: str  # unique run sentinel; guards finally-block pop against stale threads
    task: Optional[asyncio.Task]  # asyncio Task wrapping the worker thread
    triggered_by: str  # X-User-Id that started this turn (required; no empty default)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    agent: Optional[Any] = None


_active_runs: Dict[str, ActiveRun] = {}
_active_runs_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Coalescing: pending follow-up turns
#
# When @agent arrives while a turn is in flight, we record the request here
# instead of starting a second concurrent turn.  When the current turn ends
# its finally-block checks this dict and schedules one follow-up turn.
# ---------------------------------------------------------------------------

_pending_agent_turns: Dict[str, Dict[str, Any]] = {}
_pending_lock = threading.Lock()


async def _backfill_assistant(
    db_factory: Callable, session_id: str, content: str
) -> None:
    """Safety-net persist of the assistant reply.

    The agent's own GatewaySessionDB mirror normally persists the turn's
    assistant message(s) per model iteration (text/tool_calls/tool rows) —
    that full-fidelity record is what the model's follow-up context needs, and
    the UI coalesces it into one bubble on reload.

    This backfill only writes when the mirror persisted NO assistant row for
    the turn at all (the conversation-compression edge case where the agent's
    session_id rotated mid-turn). Writing unconditionally would append a
    concatenated duplicate on top of the per-iteration rows, so the live stream
    (one coalesced bubble) and the reloaded transcript would diverge.
    """
    from src.db import get_session_messages
    from src.db.store import append_message

    async with db_factory() as db:
        existing = await get_session_messages(db, session_id)
        # Find the last user message; an assistant row after it means the
        # mirror already captured this turn — nothing to backfill.
        last_user_idx = -1
        for i, m in enumerate(existing):
            if m.get("role") == "user":
                last_user_idx = i
        has_assistant_this_turn = any(
            m.get("role") == "assistant" for m in existing[last_user_idx + 1 :]
        )
        if not has_assistant_this_turn:
            await append_message(db, session_id, role="assistant", content=content)


async def _run_agent_turn_async(
    *,
    run_id: str,
    session_id: str,
    triggered_by: str,
    db_factory: Callable,
    loop: asyncio.AbstractEventLoop,
    translator: HermesSSETranslator,
    **kwargs: Any,
) -> None:
    """Async Task wrapper for the blocking turn executor.

    Runs _run_agent_turn in a thread pool and catches asyncio.CancelledError
    so the cancel endpoint can stop a turn mid-flight: partial tokens are
    flushed to the DB, turn.stopped is published to the bus, and the session
    slot is freed immediately.

    run_id is propagated to _run_agent_turn's finally block so that the old
    thread cannot pop a newer turn's _active_runs entry after early cleanup.
    """
    cancelled = False
    try:
        await loop.run_in_executor(
            None,
            functools.partial(
                _run_agent_turn,
                run_id=run_id,
                session_id=session_id,
                db_factory=db_factory,
                loop=loop,
                translator=translator,
                **kwargs,
            ),
        )
    except asyncio.CancelledError:
        cancelled = True
        partial = translator.mark_stopped()

        message_id: Optional[str] = None
        if partial.strip():
            try:
                from src.db.store import append_message

                async with db_factory() as db:
                    mid = await append_message(
                        db,
                        session_id=session_id,
                        role="assistant",
                        content=partial,
                        finish_reason="stopped",
                    )
                    message_id = str(mid) if mid is not None else None
            except Exception:
                logger.exception(
                    "agent_dispatch: failed to persist stopped message for %s",
                    session_id,
                )

        # Discard any coalesced pending turn — it would start right after cancel.
        with _pending_lock:
            _pending_agent_turns.pop(session_id, None)

        # Stopped-turn cost emission (Decision B1): emit accumulated partial
        # token counts with stopped=True. Grab whatever the agent accumulated
        # before the interrupt; fails silently if the agent wasn't constructed.
        with _active_runs_lock:
            _stopped_run = _active_runs.get(session_id)
        _stopped_agent = _stopped_run.agent if _stopped_run is not None else None
        _stopped_model = kwargs.get("model", "unknown")
        _stopped_user_id = kwargs.get("user_id", triggered_by)
        _stopped_org_id = kwargs.get("org_id")
        try:
            await emit_turn_cost(
                session_id,
                _stopped_user_id,
                _stopped_model,
                input_tokens=getattr(_stopped_agent, "session_input_tokens", 0),
                output_tokens=getattr(_stopped_agent, "session_output_tokens", 0),
                cache_read_tokens=getattr(
                    _stopped_agent, "session_cache_read_tokens", 0
                ),
                cache_write_tokens=getattr(
                    _stopped_agent, "session_cache_write_tokens", 0
                ),
                stopped=True,
                turn_id=run_id,
                org_id=_stopped_org_id,
                source_label=kwargs.get("feature_id")
                or kwargs.get("workspace_id")
                or session_id,
            )
        except Exception:
            logger.exception(
                "agent_dispatch: stopped-turn cost emission failed for session %s",
                session_id,
            )

        get_bus().publish(
            session_id,
            {
                "event": "turn.stopped",
                "data": {
                    "session_id": session_id,
                    "message_id": message_id,
                },
            },
        )
        # Do NOT re-raise — the task exits cleanly after cancellation.
    finally:
        if cancelled:
            # Free the session slot so a new turn can start immediately.
            # Guard with run_id: the still-running thread's finally will also try
            # to pop — it must not remove a newer turn's entry.
            with _active_runs_lock:
                run = _active_runs.get(session_id)
                if run is not None and run.run_id == run_id:
                    _active_runs.pop(session_id, None)


# ---------------------------------------------------------------------------
# Worker-thread agent executor
# ---------------------------------------------------------------------------


def _run_agent_turn(
    *,
    run_id: str,
    session_id: str,
    message: str,
    history: list,
    workspace_id: str,
    feature_id: str,
    user_id: str,
    model: str,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    db_factory: Callable,
    loop: asyncio.AbstractEventLoop,
    translator: HermesSSETranslator,
    org_id: Optional[str] = None,
    author_id: Optional[str] = None,
    skip_user_persist: bool = False,
    cancel_event: Optional[threading.Event] = None,
) -> None:
    """Run one blocking agent turn on a worker thread, streaming via *translator*.

    Handles the full run lifecycle including coalescing pending follow-up turns.

    *cancel_event* is set by the cancel endpoint. The thread interrupts the agent
    when it fires, stops mirroring writes to the DB, and skips the backfill so a
    cancelled turn does not finish and persist its reply behind the user's back.
    """

    def _is_cancelled() -> bool:
        return cancel_event is not None and cancel_event.is_set()

    workflow_context = None
    try:
        from plugins import context as workflow_context

        workflow_context.set_context(
            session_id,
            workspace_id,
            feature_id,
            user_id=user_id or "",
            org_id=org_id or "",
        )
        workflow_context.set_agent_context(session_id, loop, db_factory)

        # Input scope guard — enforce shared.md's "stay on-topic" rule before
        # the agent runs. Confidently off-topic messages get the canned decline
        # without invoking the agent at all. Fails open (see scope_guard).
        from src.api.scope_guard import SCOPE_DECLINE, is_out_of_scope

        # Only gate scope on the opening message of a session. `history` already
        # includes the just-persisted user message, so len<=1 means first turn.
        # Off-topic risk is highest at entry; once a workspace conversation is
        # established the classifier almost always returns IN, so running it on
        # every follow-up just adds a serialized ~1s LLM round-trip to TTFT.
        is_first_turn = len(history or []) <= 1
        if is_first_turn and is_out_of_scope(
            message, provider=provider, model=model, api_key=api_key, base_url=base_url
        ):
            logger.info(
                "agent_dispatch: declining out-of-scope message for session %s",
                session_id,
            )
            _make_delta_callback(translator.on_delta)(SCOPE_DECLINE)

            async def _persist_decline() -> None:
                from src.db.store import append_message

                async with db_factory() as db:
                    if not skip_user_persist:
                        await append_message(
                            db,
                            session_id,
                            role="user",
                            content=message,
                            author_id=author_id,
                        )
                    await append_message(
                        db, session_id, role="assistant", content=SCOPE_DECLINE
                    )

            try:
                asyncio.run_coroutine_threadsafe(_persist_decline(), loop).result(
                    timeout=15
                )
            except Exception:
                logger.exception(
                    "agent_dispatch: scope-decline persist failed for session %s",
                    session_id,
                )
            return  # finally-block runs: done(), clear context, release run, coalesce

        # Pre-turn quota guard (G8): reject before any tokens are spent.
        # Fails open — if the BFF is unreachable the turn proceeds normally.
        try:
            quota: QuotaCheckResult = asyncio.run_coroutine_threadsafe(
                check_quota(session_id, user_id, org_id=org_id),
                loop,
            ).result(timeout=5)
        except Exception:
            logger.exception(
                "agent_dispatch: quota check timed out for session %s (fail-open)",
                session_id,
            )
            quota = QuotaCheckResult.fail_open()

        if not quota.allowed:
            reason = quota.reason or "quota_exceeded"
            resets_at = quota.resets_at or ""
            if reason == "daily_exceeded":
                cap_label = (
                    f"daily credit limit ({quota.daily_cap})"
                    if quota.daily_cap
                    else "daily credit limit"
                )
                block_msg = (
                    f"You've reached your {cap_label}. "
                    f"Your quota resets at {resets_at}."
                    if resets_at
                    else f"You've reached your {cap_label}."
                )
            elif reason == "weekly_exceeded":
                cap_label = (
                    f"weekly credit limit ({quota.weekly_cap})"
                    if quota.weekly_cap
                    else "weekly credit limit"
                )
                block_msg = (
                    f"You've reached your {cap_label}. "
                    f"Your quota resets at {resets_at}."
                    if resets_at
                    else f"You've reached your {cap_label}."
                )
            else:
                block_msg = (
                    f"You've reached your credit limit. "
                    f"Your quota resets at {resets_at}."
                    if resets_at
                    else "You've reached your credit limit."
                )
            logger.info(
                "agent_dispatch: quota blocked turn for session %s reason=%s",
                session_id,
                reason,
            )

            async def _persist_quota_block() -> None:
                from src.db.store import append_message

                async with db_factory() as db:
                    if not skip_user_persist:
                        await append_message(
                            db,
                            session_id,
                            role="user",
                            content=message,
                            author_id=author_id,
                        )
                    await append_message(
                        db, session_id, role="system", content=block_msg
                    )

            try:
                asyncio.run_coroutine_threadsafe(_persist_quota_block(), loop).result(
                    timeout=15
                )
            except Exception:
                logger.exception(
                    "agent_dispatch: quota block persist failed for session %s",
                    session_id,
                )
            # Stream the quota message so the UI renders it without requiring a reload.
            _make_delta_callback(translator.on_delta)(block_msg)
            return  # zero tokens consumed; composer stays enabled

        try:
            from src.db.session_db_proxy import make_gateway_session_db

            session_db = make_gateway_session_db(
                loop,
                db_factory,
                session_id,
                author_id=author_id,
                skip_user_persist=skip_user_persist,
                is_cancelled=_is_cancelled,
            )
        except Exception:
            logger.exception(
                "agent_dispatch: gateway session DB unavailable for %s; transcript not mirrored",
                session_id,
            )
            session_db = None

        from plugins.skills import get_shared_rules

        shared_rules = get_shared_rules() or None

        _reasoning_effort = (
            os.environ.get("HERMES_REASONING_EFFORT", "medium").strip().lower()
        )
        _reasoning_off = _reasoning_effort in ("", "off", "none", "disabled")
        reasoning_config = (
            None
            if (_reasoning_off or _is_trivial_message(message))
            else {"enabled": True, "effort": _reasoning_effort}
        )

        from run_agent import AIAgent

        agent = AIAgent(
            model=model,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            enabled_toolsets=["workflow"],
            max_iterations=int(os.environ.get("HERMES_MAX_ITERATIONS", "90")),
            quiet_mode=True,
            platform="workflow_gateway",
            ephemeral_system_prompt=shared_rules,
            session_id=session_id,
            user_id=user_id or None,
            gateway_session_key=session_id,
            session_db=session_db,
            stream_delta_callback=_make_delta_callback(translator.on_delta),
            tool_start_callback=translator.on_tool_start,
            tool_complete_callback=translator.on_tool_complete,
            reasoning_callback=_make_delta_callback(translator.on_reasoning),
            reasoning_config=reasoning_config,
        )

        # Publish the live agent so the cancel endpoint can interrupt the
        # in-flight LLM call (socket-level abort). Guard with run_id so a stale
        # thread can't attach itself to a newer turn's entry. If cancel already
        # fired during setup (before the agent existed), honour it now.
        with _active_runs_lock:
            run = _active_runs.get(session_id)
            if run is not None and run.run_id == run_id:
                run.agent = agent
        if _is_cancelled():
            agent.interrupt()

        agent.run_conversation(message, conversation_history=history)

        if _is_cancelled():
            logger.info(
                "agent_dispatch: turn cancelled for session %s; skipping backfill",
                session_id,
            )
            return  # finally-block still runs: done() (suppressed), context, cleanup

        # Post-turn cost emission: read the normalized usage block from the agent
        # and emit to the BFF. Errors are logged but never block the turn.
        try:
            asyncio.run_coroutine_threadsafe(
                emit_turn_cost(
                    session_id,
                    user_id,
                    model,
                    input_tokens=getattr(agent, "session_input_tokens", 0),
                    output_tokens=getattr(agent, "session_output_tokens", 0),
                    cache_read_tokens=getattr(agent, "session_cache_read_tokens", 0),
                    cache_write_tokens=getattr(agent, "session_cache_write_tokens", 0),
                    stopped=False,
                    turn_id=run_id,
                    org_id=org_id,
                    source_label=feature_id or workspace_id or session_id,
                ),
                loop,
            ).result(timeout=15)
        except Exception:
            logger.exception(
                "agent_dispatch: post-turn cost emission failed for session %s",
                session_id,
            )

        final_text = (getattr(translator, "full_text", "") or "").strip()
        if final_text:
            try:
                fut = asyncio.run_coroutine_threadsafe(
                    _backfill_assistant(db_factory, session_id, final_text), loop
                )
                fut.result(timeout=15)
            except Exception:
                logger.exception(
                    "agent_dispatch: assistant backfill failed for session %s",
                    session_id,
                )
    except Exception as exc:
        logger.exception("agent_dispatch: agent turn failed for session %s", session_id)
        translator.on_error(str(exc))
    finally:
        translator.done()
        if workflow_context is not None:
            workflow_context.clear_context(session_id)
        # Guard with run_id: if this turn was cancelled, _run_agent_turn_async
        # already freed the slot and a new turn may have registered. Only pop if
        # the current dict entry still belongs to this run.
        with _active_runs_lock:
            run = _active_runs.get(session_id)
            if run is not None and run.run_id == run_id:
                _active_runs.pop(session_id, None)

        # Coalescing: if a follow-up @agent mention arrived while we were running,
        # schedule exactly one new turn now (not N).
        with _pending_lock:
            pending = _pending_agent_turns.pop(session_id, None)

        if pending:
            asyncio.run_coroutine_threadsafe(
                _schedule_follow_up(session_id, pending, loop),
                loop,
            )


async def _schedule_follow_up(
    session_id: str,
    pending: Dict[str, Any],
    loop: asyncio.AbstractEventLoop,
) -> None:
    """Re-load history and schedule the coalesced follow-up turn."""
    try:
        from src.db import get_messages_as_conversation, touch_session
        from src.api.model_catalog import resolve_model

        async with pending["db_factory"]() as db:
            history = await get_messages_as_conversation(db, session_id)
            await touch_session(db, session_id)
            resolved = await resolve_model(db, pending["model"])

        with _active_runs_lock:
            if session_id in _active_runs:
                # Shouldn't happen, but guard against races.
                return

        run_id = uuid.uuid4().hex
        follow_translator = BusPublishingSSETranslator(
            session_id=session_id, model=resolved["model"]
        )
        cancel_event = threading.Event()
        get_bus().publish(
            session_id, {"event": "agent.working", "data": {"session_id": session_id}}
        )
        task = asyncio.ensure_future(
            _run_agent_turn_async(
                run_id=run_id,
                session_id=session_id,
                triggered_by=pending["user_id"],
                message=pending["message"],
                history=history,
                workspace_id=pending["workspace_id"],
                feature_id=pending["feature_id"],
                user_id=pending["user_id"],
                org_id=pending.get("org_id"),
                model=resolved["model"],
                provider=resolved["provider"],
                api_key=resolved["api_key"],
                base_url=resolved["base_url"],
                db_factory=pending["db_factory"],
                loop=loop,
                translator=follow_translator,
                skip_user_persist=True,
                cancel_event=cancel_event,
            )
        )
        with _active_runs_lock:
            _active_runs[session_id] = ActiveRun(
                run_id=run_id,
                task=task,
                triggered_by=pending["user_id"],
                cancel_event=cancel_event,
            )
    except Exception:
        logger.exception(
            "agent_dispatch: failed to schedule follow-up turn for %s", session_id
        )
        with _active_runs_lock:
            _active_runs.pop(session_id, None)


async def schedule_agent_turn(
    *,
    session_id: str,
    message: str,
    history: list,
    workspace_id: str,
    feature_id: str,
    user_id: str,
    model: str,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    db_factory: Callable,
    loop: asyncio.AbstractEventLoop,
    org_id: Optional[str] = None,
    author_id: Optional[str] = None,
    skip_user_persist: bool = False,
) -> bool:
    """Schedule an agent turn with coalescing.

    If a turn is already in flight for session_id, record a pending follow-up
    (coalescing: only one follow-up regardless of how many @agent mentions
    arrived during the in-flight turn) and return False.

    Otherwise, claim the session and schedule the turn on the thread pool;
    return True.
    """
    with _active_runs_lock:
        if session_id in _active_runs:
            # Turn already in flight — record a pending follow-up (coalesce).
            with _pending_lock:
                _pending_agent_turns[session_id] = {
                    "message": message,
                    "workspace_id": workspace_id,
                    "feature_id": feature_id,
                    "user_id": user_id,
                    "org_id": org_id,
                    "model": model,
                    "db_factory": db_factory,
                }
            logger.debug(
                "agent_dispatch: coalesced pending turn for %s (turn already in flight)",
                session_id,
            )
            return False

    run_id = uuid.uuid4().hex
    translator = BusPublishingSSETranslator(session_id=session_id, model=model)
    cancel_event = threading.Event()

    # Signal to all stream subscribers that the agent is starting work.
    get_bus().publish(
        session_id, {"event": "agent.working", "data": {"session_id": session_id}}
    )

    # Create an asyncio Task so the cancel endpoint can call task.cancel().
    # asyncio.ensure_future is synchronous — no await between the claim check and
    # the ActiveRun store, so no other coroutine can interleave here.
    task = asyncio.ensure_future(
        _run_agent_turn_async(
            run_id=run_id,
            session_id=session_id,
            triggered_by=user_id,
            message=message,
            history=history,
            workspace_id=workspace_id,
            feature_id=feature_id,
            user_id=user_id,
            org_id=org_id,
            model=model,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            db_factory=db_factory,
            loop=loop,
            translator=translator,
            author_id=author_id,
            skip_user_persist=skip_user_persist,
            cancel_event=cancel_event,
        )
    )
    with _active_runs_lock:
        _active_runs[session_id] = ActiveRun(
            run_id=run_id, task=task, triggered_by=user_id, cancel_event=cancel_event
        )
    return True
