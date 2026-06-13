"""Streaming chat route.

    POST /chat — run one agent turn and stream the reply back as SSE
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import threading
from typing import Optional, Set

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.api.model_catalog import default_model, resolve_model
from src.db import (
    get_messages_as_conversation,
    get_session,
    set_session_title,
    touch_session,
    update_session_model,
)
from src.db.session_db_proxy import make_gateway_session_db
from src.streaming import HermesSSETranslator

logger = logging.getLogger(__name__)

# Sessions with an agent run currently in flight. A second chat turn for the
# same session (e.g. a reconnect or double-submit) must not start a second run
# — both would mirror the same messages to Postgres and the transcript would
# duplicate. Guarded by a lock because the marker is removed from the agent's
# worker thread.
_active_runs: Set[str] = set()
_active_runs_lock = threading.Lock()

router = APIRouter()


class StreamChatRequest(BaseModel):
    session_id: str
    message: str
    user_id: str = ""
    workspace_id: str = ""
    feature_id: str = ""
    # Catalog model id (see model_catalog). Empty → reuse the session's model,
    # then the server default. Unknown ids fall back to the default.
    model: str = ""


def _derive_title(message: str) -> str:
    """First 60 chars of the opening message — used to auto-title a session."""
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    return first_line[:60] or "New chat"


def _run_agent_turn(
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
    db_factory,
    loop: asyncio.AbstractEventLoop,
    translator: HermesSSETranslator,
) -> None:
    """Run one blocking agent turn on a worker thread, streaming via *translator*.

    This owns the run lifecycle end-to-end: whatever happens, it finalizes the
    SSE stream (``translator.done`` / ``on_error``) and releases the in-flight
    marker so the session can accept the next turn. The HTTP handler returns as
    soon as this is scheduled — the response body is driven by the translator's
    async queue, which this function feeds from the worker thread.
    """
    workflow_context = None
    try:
        # Tool handlers and the pre_llm_call hook read the active workspace /
        # feature from this thread-local context. The executor pool is reused
        # across sessions, so it has to be (re)set at the start of every turn.
        from plugins import context as workflow_context
        workflow_context.set_context(session_id, workspace_id, feature_id)

        # Mirror the agent's transcript writes into Postgres. Best-effort: if
        # the proxy can't be built we still run the turn, just unmirrored.
        try:
            session_db = make_gateway_session_db(loop, db_factory, session_id)
        except Exception:
            logger.exception(
                "chat: gateway session DB unavailable for %s; transcript not mirrored",
                session_id,
            )
            session_db = None

        # The bundled shared workflow rules (plugins/skills/shared.md) are appended to
        # the agent's system prompt every turn so it always follows the company
        # workflow (feature lifecycle, stage-review + task statuses, the flow).
        from plugins.skills import get_shared_rules
        shared_rules = get_shared_rules() or None

        # Heavyweight agent deps are imported here so they only load when a turn
        # actually runs (and so tests can stub `run_agent` in sys.modules).
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
            stream_delta_callback=translator.on_delta,
            tool_start_callback=translator.on_tool_start,
            tool_complete_callback=translator.on_tool_complete,
        )
        agent.run_conversation(message, conversation_history=history)
    except Exception as exc:  # noqa: BLE001 — any failure must reach the client
        logger.exception("chat: agent turn failed for session %s", session_id)
        translator.on_error(str(exc))
    finally:
        translator.done()
        if workflow_context is not None:
            workflow_context.clear_context(session_id)
        with _active_runs_lock:
            _active_runs.discard(session_id)


@router.post("/chat")
async def chat(
    body: StreamChatRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Run one agent turn for a session and stream the reply back as SSE.

    ``run_conversation`` is blocking, so the turn runs on a worker thread; its
    callbacks are bridged onto an async SSE generator by
    :class:`HermesSSETranslator`. The wire format is hermes's native
    ``/v1/chat/completions`` stream.

    A session may only have one turn in flight: a concurrent request (reconnect
    or double-submit) gets a 409 so the transcript isn't persisted twice.
    """
    session_id = body.session_id
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required.")

    # Reserve the session before doing any work — reject a second concurrent run.
    with _active_runs_lock:
        if session_id in _active_runs:
            raise HTTPException(
                status_code=409,
                detail=f"Session {session_id!r} already has a turn in flight.",
            )
        _active_runs.add(session_id)

    # Until the worker thread takes ownership of the marker, any setup failure
    # here must release it or the session would stay locked forever.
    try:
        session = await get_session(db, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found.")

        history = await get_messages_as_conversation(db, session_id)

        # Resolve the model for this turn: a per-turn FE selection wins and is
        # persisted on the session; otherwise reuse the session's last model,
        # then the server default. Unknown ids fall back inside resolve_model.
        chosen = (body.model or "").strip() or getattr(session, "model", None) or default_model()
        resolved = resolve_model(chosen)
        if resolved["model"] != getattr(session, "model", None):
            await update_session_model(db, session_id, resolved["model"])

        # First turn with no title yet → derive one from the opening message.
        if not getattr(session, "title", None):
            await set_session_title(db, session_id, _derive_title(body.message))

        await touch_session(db, session_id)
    except Exception:
        with _active_runs_lock:
            _active_runs.discard(session_id)
        raise

    translator = HermesSSETranslator(model=resolved["model"])
    loop = asyncio.get_running_loop()
    loop.run_in_executor(
        None,
        functools.partial(
            _run_agent_turn,
            session_id=session_id,
            message=body.message,
            history=history,
            workspace_id=body.workspace_id,
            feature_id=body.feature_id,
            user_id=identity.user_id or body.user_id,
            model=resolved["model"],
            provider=resolved["provider"],
            api_key=resolved["api_key"],
            base_url=resolved["base_url"],
            db_factory=request.app.state.db_session,
            loop=loop,
            translator=translator,
        ),
    )

    return StreamingResponse(
        translator.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
