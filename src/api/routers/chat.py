"""Streaming chat route.

    POST /chat — run one agent turn and stream the reply back as SSE
"""

from __future__ import annotations

import asyncio
import functools
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.agent_dispatch import (
    _active_runs,
    _active_runs_lock,
    _run_agent_turn,
)
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
from src.streaming import HermesSSETranslator

logger = logging.getLogger(__name__)

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
