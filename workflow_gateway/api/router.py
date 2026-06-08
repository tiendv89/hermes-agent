"""FastAPI router for the workflow gateway.

Routes:
    POST /create_session  — create a new session
    POST /stream_chat     — run one agent turn and stream SSE back

The router is mounted at ``/api/v5`` in ``workflow_gateway/app.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, AsyncIterator, Dict

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from workflow_gateway.db import (
    create_session,
    get_messages_as_conversation,
    get_session,
    list_sessions,
    set_session_title,
    touch_session,
)
from workflow_gateway.db.session_db_proxy import make_gateway_session_db
from workflow_gateway.streaming import HermesSSETranslator

logger = logging.getLogger(__name__)

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
    user_id: str
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
# POST /create_session
# ---------------------------------------------------------------------------


@router.post("/create_session", response_model=CreateSessionResponse)
async def create_session_endpoint(
    body: CreateSessionRequest,
    db: AsyncSession = Depends(_get_db),
) -> CreateSessionResponse:
    session_id = await create_session(
        db,
        user_id=body.user_id,
        workspace_id=body.workspace_id,
        feature_id=body.feature_id,
    )
    logger.info("Created session %s for user %s", session_id, body.user_id)
    return CreateSessionResponse(session_id=session_id)


# ---------------------------------------------------------------------------
# GET /sessions
# ---------------------------------------------------------------------------


@router.get("/sessions")
async def list_sessions_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    feature_id: str = Query(..., description="Feature slug or ID"),
    limit: int = Query(50, ge=1, le=200, description="Max sessions to return"),
    db: AsyncSession = Depends(_get_db),
) -> JSONResponse:
    """Return non-archived sessions for a workspace+feature, newest-first."""
    sessions = await list_sessions(
        db, workspace_id=workspace_id, feature_id=feature_id, limit=limit
    )
    return JSONResponse({"sessions": sessions})


# ---------------------------------------------------------------------------
# POST /stream_chat
# ---------------------------------------------------------------------------


@router.post("/stream_chat")
async def stream_chat_endpoint(
    request: Request,
    body: StreamChatRequest,
    db: AsyncSession = Depends(_get_db),
) -> StreamingResponse:
    """Run one agent turn and stream the response as SSE."""
    session = await get_session(db, body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

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
        user_id=body.user_id,
        workspace_id=body.workspace_id,
        feature_id=body.feature_id,
    )

    translator = HermesSSETranslator()
    context_vars = {"workspace_id": body.workspace_id, "feature_id": body.feature_id}
    loop = asyncio.get_event_loop()
    db_factory = request.app.state.db_session

    def _run_agent() -> None:
        """Blocking agent run — executed in a thread pool."""
        try:
            from run_agent import AIAgent

            model = os.environ.get("HERMES_MODEL", "claude-sonnet-4-6")
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

            agent._context_vars = context_vars  # type: ignore[attr-defined]

            agent.run_conversation(
                body.message,
                conversation_history=conversation_history,
            )

        except Exception as exc:
            logger.exception("Agent run failed for session %s", body.session_id)
            translator.on_error(str(exc))
        finally:
            translator.done()

    loop.run_in_executor(None, _run_agent)

    return StreamingResponse(
        translator.stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
