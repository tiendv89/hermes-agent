"""FastAPI router for the workflow gateway.

Routes:
    POST /create_session  — create a new Postgres-backed session
    POST /stream_chat     — run one agent turn and stream SSE back

The router is mounted at ``/api/v5`` in ``workflow_gateway/app.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from workflow_gateway.auth import verify_token
from workflow_gateway.sessions import (
    NoOpSessionDB,
    create_session,
    get_session,
    get_messages,
    append_message,
    touch_session,
)
from workflow_gateway.streaming import HermesSSETranslator

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class CreateSessionRequest(BaseModel):
    user_id: str


class CreateSessionResponse(BaseModel):
    session_id: str


class StreamChatRequest(BaseModel):
    session_id: str
    message: str
    workspace_id: str = ""
    feature_id: str = ""


# ---------------------------------------------------------------------------
# POST /create_session
# ---------------------------------------------------------------------------

@router.post("/create_session", response_model=CreateSessionResponse)
async def create_session_endpoint(
    body: CreateSessionRequest,
    request: Request,
    token: Optional[str] = Depends(verify_token),
) -> CreateSessionResponse:
    """Create a new Postgres-backed session and return its session_id."""
    pool = request.app.state.db_pool
    session_id = await create_session(
        pool=pool,
        user_id=body.user_id,
    )
    logger.info("Created session %s for user %s", session_id, body.user_id)
    return CreateSessionResponse(session_id=session_id)


# ---------------------------------------------------------------------------
# POST /stream_chat
# ---------------------------------------------------------------------------

@router.post("/stream_chat")
async def stream_chat_endpoint(
    body: StreamChatRequest,
    request: Request,
    token: Optional[str] = Depends(verify_token),
) -> StreamingResponse:
    """Run one agent turn and stream the response as SSE.

    The agent is spawned in a thread (AIAgent is synchronous). Callbacks
    feed a HermesSSETranslator which is consumed as an async generator.
    """
    pool = request.app.state.db_pool

    # Validate session exists.
    session = await get_session(pool, body.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    # Load conversation history from Postgres.
    history_rows = await get_messages(pool, body.session_id)
    conversation_history = [
        {"role": r["role"], "content": r["content"]} for r in history_rows
    ]

    # Persist the new user message.
    await append_message(pool, body.session_id, "user", body.message)

    # Touch last_active_at.
    await touch_session(pool, body.session_id)

    # SSE translator — bridges the synchronous AIAgent callbacks to async.
    translator = HermesSSETranslator()

    # Context vars passed to the pre_llm_call hook via AIAgent kwargs.
    context_vars = {
        "workspace_id": body.workspace_id,
        "feature_id": body.feature_id,
    }

    loop = asyncio.get_event_loop()

    def _run_agent() -> None:
        """Blocking agent run — executed in a thread pool."""
        try:
            from run_agent import AIAgent

            model = os.environ.get("HERMES_MODEL", "claude-sonnet-4-6")
            provider = os.environ.get("HERMES_PROVIDER", "anthropic")

            agent = AIAgent(
                model=model,
                provider=provider,
                session_id=body.session_id,
                session_db=NoOpSessionDB(),
                stream_delta_callback=translator.on_delta,
                tool_start_callback=translator.on_tool_start,
                tool_complete_callback=translator.on_tool_complete,
            )

            # Inject context vars so pre_llm_call hook can read them.
            agent._context_vars = context_vars  # type: ignore[attr-defined]

            agent.run_conversation(
                body.message,
                conversation_history=conversation_history,
            )

            # Emit usage if available.
            usage = getattr(agent, "_last_usage", None)
            if usage:
                translator.on_usage(**usage)

        except Exception as exc:
            logger.exception("Agent run failed for session %s", body.session_id)
            translator.on_error(str(exc))
        finally:
            translator.done()

    # Spawn agent in a thread so we don't block the event loop.
    loop.run_in_executor(None, _run_agent)

    return StreamingResponse(
        translator.stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
