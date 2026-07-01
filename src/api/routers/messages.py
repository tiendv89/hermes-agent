"""Send service — POST /api/v1/threads/{session_id}/messages (v4 team-chat).

Decouples human-message persistence from the agent turn:
  1. Persist the human message with author_id.
  2. Parse + resolve @mentions; persist message_mentions.
  3. Gate agent dispatch per trigger rules (§4.2):
       - Explicit @agent mention → trigger.
       - Feature thread + bare message (no @agent) → trigger (v3 feel preserved).
       - Channel + bare message → no trigger.
  4. If triggered: schedule an agent turn with coalescing (via agent_dispatch).
  5. Return 202 immediately (fire-and-forget pattern).
"""

from __future__ import annotations

import asyncio
import logging
import time as _time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.agent_dispatch import schedule_agent_turn
from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.api.mentions import parse_mention_handles, resolve_mentions
from src.api.model_catalog import default_model, resolve_model
from src.db import (
    get_messages_as_conversation,
    get_messages_since,
    get_session,
    get_session_messages,
    is_member,
    persist_mentions,
    set_session_title,
    touch_session,
    update_session_model,
)
from src.db.store import append_message
from src.realtime.bus import get_bus
from src.services.author_resolver import attach_authors, author_for, mention_candidates

logger = logging.getLogger(__name__)

router = APIRouter()


class SendMessageRequest(BaseModel):
    content: str
    # Optional model override (same semantics as legacy /chat).
    model: str = ""


def _should_trigger_agent(session, has_explicit_agent_mention: bool) -> bool:
    """Dispatch gate: decide whether this message should start an agent turn.

    Rules (§4.2, resolved):
      - Explicit @agent mention → always trigger.
      - Bare message in a feature thread (feature_id != '') → trigger (v3 feel).
      - Bare message in a channel (kind='channel') → never trigger.
    """
    if has_explicit_agent_mention:
        return True
    # Bare message: only trigger in feature threads.
    feature_id = getattr(session, "feature_id", "") or ""
    kind = getattr(session, "kind", "thread") or "thread"
    return kind == "thread" and bool(feature_id)


@router.get("/threads/{session_id}/messages")
async def get_thread_messages(
    session_id: str,
    since: str = Query("", description="Return only messages after this message id (cursor)"),
    _identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return a thread/channel transcript, oldest-first.

    Threads and channels are sessions, so this reuses the session transcript
    store. ``since`` (a numeric message id) returns only newer messages — the
    catch-up cursor used by the live subscription transport.
    """
    if since:
        try:
            since_id = int(since)
        except ValueError:
            since_id = 0
        messages = await get_messages_since(db, session_id, since_id)
    else:
        messages = await get_session_messages(db, session_id)

    # Enrich author display info (name/avatar) from user-service so the channel
    # transcript shows real names rather than raw ids.
    session = await get_session(db, session_id)
    workspace_id = getattr(session, "workspace_id", "") or "" if session else ""
    await attach_authors(workspace_id, messages)
    return JSONResponse({"messages": messages})


@router.post("/threads/{session_id}/messages", status_code=202)
async def send_message(
    session_id: str,
    body: SendMessageRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Persist a human message, resolve @mentions, and gate the agent turn.

    Returns 202 immediately; the agent turn (if triggered) runs in the background.
    """
    if not body.content or not body.content.strip():
        raise HTTPException(status_code=400, detail="content must be non-empty.")

    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    # Verify the session exists.
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Thread not found.")

    # Verify caller is a member (owner or explicit member).
    owner_id = getattr(session, "user_id", None) or ""
    caller_is_member = (user_id == owner_id) or await is_member(db, session_id, user_id)
    if not caller_is_member:
        raise HTTPException(status_code=403, detail="Not a member of this thread.")

    ws_id = getattr(session, "workspace_id", "") or ""

    # --- Mention parse + resolve ---
    handles = parse_mention_handles(body.content)
    has_agent_mention = "agent" in handles

    # Resolve @handles against the whole workspace directory (user-service), so
    # any workspace member can be mentioned — not only current channel members.
    resolved_mentions = resolve_mentions(handles, await mention_candidates(ws_id))

    # --- Persist the human message (with author_id) ---
    message_id = await append_message(
        db,
        session_id=session_id,
        role="user",
        content=body.content,
        author_id=user_id,
    )

    # --- Persist resolved mentions ---
    if resolved_mentions:
        await persist_mentions(
            db,
            message_id=message_id,
            session_id=session_id,
            mentions=resolved_mentions,
        )

    await touch_session(db, session_id)

    # Auto-title the session from the first message if it has no title yet.
    if not getattr(session, "title", None):
        first_line = body.content.strip().splitlines()[0] if body.content.strip() else ""
        await set_session_title(db, session_id, first_line[:60] or "New chat")

    # --- Fan-out to SSE stream subscribers ---
    # Resolve author display info so other subscribers see the sender's name.
    author = await author_for(ws_id, user_id)
    get_bus().publish(
        session_id,
        {
            "event": "message.created",
            "data": {
                "id": str(message_id),
                "session_id": session_id,
                "role": "user",
                "content": body.content,
                "author_id": user_id,
                "author": author,
                "created_at": _time.time(),
                "mentions": resolved_mentions,
            },
        },
    )

    # --- Dispatch gate ---
    if not _should_trigger_agent(session, has_agent_mention):
        return JSONResponse(
            {"status": "accepted", "message_id": message_id, "agent_triggered": False},
            status_code=202,
        )

    # --- Trigger agent (with coalescing) ---
    chosen_model = (
        (body.model or "").strip() or getattr(session, "model", None) or await default_model(db)
    )
    resolved = await resolve_model(db, chosen_model)
    if resolved["model"] != getattr(session, "model", None):
        await update_session_model(db, session_id, resolved["model"])

    # Load conversation history (which now includes the pre-persisted user message).
    history = await get_messages_as_conversation(db, session_id)

    loop = asyncio.get_running_loop()
    workspace_id = ws_id
    feature_id = getattr(session, "feature_id", "") or ""

    await schedule_agent_turn(
        session_id=session_id,
        message=body.content,
        history=history,
        workspace_id=workspace_id,
        feature_id=feature_id,
        user_id=user_id,
        org_id=identity.org_id or None,
        model=resolved["model"],
        provider=resolved["provider"],
        api_key=resolved["api_key"],
        base_url=resolved["base_url"],
        db_factory=request.app.state.db_session,
        loop=loop,
        author_id=user_id,
        skip_user_persist=True,
    )

    return JSONResponse(
        {
            "status": "accepted",
            "message_id": message_id,
            "agent_triggered": True,
            "agent_mentions": [
                m for m in resolved_mentions if m["mentioned_kind"] == "agent"
            ],
        },
        status_code=202,
    )
