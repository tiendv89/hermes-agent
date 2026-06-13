"""FastAPI router for the workflow gateway.

Routes:
    POST /session — create a new session
    GET /sessions — list sessions for a workspace+feature
    GET /sessions/{session_id}/messages — load a session's transcript
    POST /chat — run one agent turn and stream SSE back

The router is mounted at ``/api/v1`` in ``src/app.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import AsyncIterator, Set

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import (
    create_session,
    get_messages_as_conversation,
    get_session,
    get_session_messages,
    list_sessions,
    set_session_title,
    touch_session,
)
from src.api.identity import Identity, require_identity
from src.db.session_db_proxy import make_gateway_session_db
from src.streaming import HermesSSETranslator

logger = logging.getLogger(__name__)

# Sessions with an agent run currently in flight. A second stream_chat for the
# same session (e.g. a reconnect or double-submit) must not start a second run
# — both would mirror the same messages to Postgres and the transcript would
# duplicate. Guarded by a lock because the marker is removed from the agent's
# worker thread.
_active_runs: Set[str] = set()
_active_runs_lock = threading.Lock()

router = APIRouter()


# ---------------------------------------------------------------------------
# DB dependency
# ---------------------------------------------------------------------------


async def _get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.db_session() as session:
        yield session


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateSessionRequest(BaseModel):
    # Identity is taken from the BFF-injected X-User-Id header, not the body.
    # user_id is kept (optional) only as a fallback for direct/local calls.
    user_id: str = ""
    workspace_id: str = ""
    feature_id: str = ""


class CreateSessionResponse(BaseModel):
    session_id: str


class StreamChatRequest(BaseModel):
    session_id: str
    message: str
    user_id: str = ""
    workspace_id: str = ""
    feature_id: str = ""


# ---------------------------------------------------------------------------
# POST /session
# ---------------------------------------------------------------------------


@router.post("/session", response_model=CreateSessionResponse)
async def create_session_endpoint(
    body: CreateSessionRequest,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(_get_db),
) -> CreateSessionResponse:
    user_id = identity.user_id or body.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")
    session_id = await create_session(
        db,
        user_id=user_id,
        workspace_id=body.workspace_id,
        feature_id=body.feature_id,
    )
    logger.info("Created session %s for user %s", session_id, user_id)
    return CreateSessionResponse(session_id=session_id)


# ---------------------------------------------------------------------------
# GET /sessions
# ---------------------------------------------------------------------------


@router.get("/sessions")
async def list_sessions_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    feature_id: str = Query(..., description="Feature slug or ID"),
    limit: int = Query(50, ge=1, le=200, description="Max sessions to return"),
    _identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(_get_db),
) -> JSONResponse:
    """Return non-archived sessions for a workspace+feature, newest-first."""
    sessions = await list_sessions(
        db, workspace_id=workspace_id, feature_id=feature_id, limit=limit
    )
    return JSONResponse({"sessions": sessions})


# ---------------------------------------------------------------------------
# GET /sessions/{session_id}/messages
# ---------------------------------------------------------------------------


@router.get("/sessions/{session_id}/messages")
async def get_session_messages_endpoint(
    session_id: str,
    _identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(_get_db),
) -> JSONResponse:
    """Return the full transcript for a session, oldest-first."""
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")
    messages = await get_session_messages(db, session_id)
    return JSONResponse({"session_id": session_id, "messages": messages})


# ---------------------------------------------------------------------------
# POST /chat
# ---------------------------------------------------------------------------


@router.post("/chat")
async def stream_chat_endpoint(
    request: Request,
    body: StreamChatRequest,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(_get_db),
) -> StreamingResponse:
    """Run one agent turn and stream the response as SSE."""
    user_id = identity.user_id or body.user_id
    session = await get_session(db, body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    # Reject a second concurrent run for this session: it would re-run the agent
    # and double-persist every message (duplicated transcript on reload).
    with _active_runs_lock:
        if body.session_id in _active_runs:
            raise HTTPException(
                status_code=409,
                detail="A response is already streaming for this session.",
            )
        _active_runs.add(body.session_id)

    # Auto-title: set the session title to the first 60 chars of the message
    # when the session has no title yet.
    if not session.title and body.message:
        await set_session_title(db, body.session_id, body.message[:60])
        # Refresh the session object so downstream code sees the updated title.
        session = await get_session(db, body.session_id)

    conversation_history = await get_messages_as_conversation(db, body.session_id)
    await touch_session(
        db,
        body.session_id,
        user_id=user_id,
        workspace_id=body.workspace_id,
        feature_id=body.feature_id,
    )

    model = os.environ.get("HERMES_MODEL", "claude-sonnet-4-6")
    translator = HermesSSETranslator(model=model)
    # Prefer the values stored on the session (set at create_session time).
    # The request body may omit them; the session row is the authoritative source.
    workspace_id = session.workspace_id or body.workspace_id
    feature_id = session.feature_id or body.feature_id
    logger.info(
        "stream_chat session=%s resolved workspace_id=%r feature_id=%r "
        "(session row: %r/%r, request body: %r/%r)",
        body.session_id, workspace_id, feature_id,
        session.workspace_id, session.feature_id,
        body.workspace_id, body.feature_id,
    )
    loop = asyncio.get_event_loop()
    db_factory = request.app.state.db_session

    # Mutable handle so the SSE generator (event loop) can interrupt the agent
    # (worker thread) when the client disconnects.
    agent_ref: list = [None]

    def _run_agent() -> None:
        """Blocking agent run — executed in a thread pool."""
        try:
            from run_agent import AIAgent

            provider = os.environ.get("HERMES_PROVIDER", "anthropic")

            # GatewaySessionDB mirrors every append_message / update_token_counts
            # call hermes makes internally into the gateway's Postgres store.
            session_db = make_gateway_session_db(loop, db_factory, body.session_id)

            agent = AIAgent(
                model=model,
                provider=provider,
                session_id=body.session_id,
                session_db=session_db,
                stream_delta_callback=translator.on_delta,
                tool_start_callback=translator.on_tool_start,
                tool_complete_callback=translator.on_tool_complete,
            )
            agent_ref[0] = agent

            # Publish workspace/feature IDs so the workflow plugin can resolve
            # them: the pre_llm_call hook looks them up by session_id, and tool
            # handlers fall back to the thread-local — both set here.
            try:
                from plugins.context import set_context
                set_context(body.session_id, workspace_id, feature_id)
            except Exception:
                logger.warning("Failed to set workflow context", exc_info=True)

            agent.run_conversation(
                body.message,
                conversation_history=conversation_history,
            )

        except Exception as exc:
            logger.exception("Agent run failed for session %s", body.session_id)
            translator.on_error(str(exc))
        finally:
            try:
                from plugins.context import clear_context
                clear_context(body.session_id)
            except Exception:
                pass
            with _active_runs_lock:
                _active_runs.discard(body.session_id)
            translator.done()

    loop.run_in_executor(None, _run_agent)

    async def _sse_body() -> AsyncIterator[str]:
        """Forward translator frames; interrupt the agent if the client leaves."""
        try:
            async for chunk in translator.stream():
                yield chunk
        finally:
            # Normal completion: the agent is already done, interrupt is a no-op.
            # Client disconnect (GeneratorExit): stop the still-running agent so
            # it doesn't keep working and re-persist a transcript nobody is
            # watching.
            agent = agent_ref[0]
            if agent is not None and hasattr(agent, "interrupt"):
                try:
                    agent.interrupt("SSE client disconnected")
                except Exception:
                    logger.debug("agent.interrupt failed", exc_info=True)

    return StreamingResponse(
        _sse_body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
