"""Message-thread reply endpoints (G2 — post into / read a message thread).

POST /threads/{session_id}/messages/{message_id}/replies
    Post a new reply into the thread rooted at message_id. The root message
    must itself have thread_root_id IS NULL (no nested threads). Reuses the
    entire existing pipeline: mention resolution, SSE fan-out, agent-dispatch
    gate.

GET /threads/{session_id}/messages/{message_id}/replies?since=
    Return thread replies oldest-first, author-enriched. Supports the same
    ?since= cursor used by the main transcript's SSE catch-up.
"""

from __future__ import annotations

import asyncio
import time as _time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.agent_dispatch import schedule_agent_turn, try_resolve_pending_clarify
from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.api.mentions import parse_mention_handles, resolve_mentions
from src.api.model_catalog import resolve_model
from src.api.routers.messages import _image_urls_for, _should_trigger_agent
from src.api.thread_authz import authorize_thread_access
from src.db import (
    Message,
    add_member,
    get_session,
    get_thread_messages_as_conversation,
    persist_mentions,
    touch_session,
    update_session_model,
)
from src.db.store import append_message, get_thread_replies
from src.realtime.bus import get_bus
from src.services.author_resolver import attach_authors, author_for, mention_candidates

router = APIRouter()


class PostThreadReplyRequest(BaseModel):
    content: str
    model: str = ""
    # When the user replies to a specific message *inside* the thread panel,
    # this is the id of that specific reply (not the root). Optional.
    reply_to_message_id: str | None = None
    # IDs of images uploaded to storage-service's images bucket the user
    # attached to this reply (see messages.py's SendMessageRequest.image_ids).
    image_ids: list[str] = []


@router.post("/threads/{session_id}/messages/{message_id}/replies", status_code=202)
async def post_thread_reply(
    session_id: str,
    message_id: str,
    body: PostThreadReplyRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Post a reply into the message thread rooted at message_id."""
    # Empty text is fine for an image-only send — must have at least one or the other.
    if (not body.content or not body.content.strip()) and not body.image_ids:
        raise HTTPException(status_code=400, detail="content or image_ids must be non-empty.")

    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    # Parse and validate the root message id.
    try:
        root_id = int(message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="message_id must be a numeric id.")

    # Verify the session exists.
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Thread not found.")

    ws_id = getattr(session, "workspace_id", "") or ""

    # Sessions (kind='thread') are org-public like channels — any org member is
    # authorized to reply even without an explicit session_members row.
    caller_is_workspace_member, org_id = await authorize_thread_access(
        db, session, identity.user_id, identity.org_id
    )
    kind_val = getattr(session, "kind", "thread") or "thread"

    # Implicit join for authorized org members on any thread session — idempotent.
    if kind_val == "thread" and caller_is_workspace_member:
        await add_member(db, session_id, user_id, added_by=user_id)

    # Load the root message and validate it belongs to this session and is itself
    # a top-level message (thread_root_id IS NULL — no nested threads).
    root_msg = await db.get(Message, root_id)
    if root_msg is None or root_msg.session_id != session_id:
        raise HTTPException(status_code=404, detail="Message not found.")
    if root_msg.thread_root_id is not None:
        raise HTTPException(
            status_code=400,
            detail="nested_thread_not_supported",
        )

    # Resolve optional reply_to_message_id (a specific message within the thread).
    inner_reply_to_id: int | None = None
    if body.reply_to_message_id:
        try:
            inner_reply_to_id = int(body.reply_to_message_id)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="reply_to_message_id must be a numeric id."
            )
    else:
        # Default: the reply is to the root message itself.
        inner_reply_to_id = root_id

    # --- Mention parse + resolve ---
    handles = parse_mention_handles(body.content)
    has_agent_mention = "agent" in handles
    resolved_mentions = resolve_mentions(handles, await mention_candidates(org_id))

    # --- Persist the thread reply ---
    new_message_id = await append_message(
        db,
        session_id=session_id,
        role="user",
        content=body.content,
        author_id=user_id,
        thread_root_id=root_id,
        reply_to_message_id=inner_reply_to_id,
        image_ids=body.image_ids,
    )

    # --- Persist resolved mentions ---
    if resolved_mentions:
        await persist_mentions(
            db,
            message_id=new_message_id,
            session_id=session_id,
            mentions=resolved_mentions,
            content=body.content,
            author_id=user_id,
        )

    await touch_session(db, session_id)

    # --- Fan-out to SSE stream subscribers ---
    author = await author_for(ws_id, user_id)
    get_bus().publish(
        session_id,
        {
            "event": "message.created",
            "data": {
                "id": str(new_message_id),
                "session_id": session_id,
                "role": "user",
                "content": body.content,
                "author_id": user_id,
                "author": author,
                "created_at": _time.time(),
                "mentions": resolved_mentions,
                "thread_root_id": str(root_id),
                "reply_to_message_id": str(inner_reply_to_id),
                "image_urls": _image_urls_for(ws_id, body.image_ids) if body.image_ids else [],
            },
        },
    )

    # --- Clarify resolution ---
    # See messages.py's send_message for why this must be checked before the
    # dispatch gate below (agent_dispatch.try_resolve_pending_clarify).
    if try_resolve_pending_clarify(session_id, user_id, body.content):
        return JSONResponse(
            {"status": "accepted", "message_id": str(new_message_id), "agent_triggered": False},
            status_code=202,
        )

    # --- Dispatch gate (unchanged — G5) ---
    if not _should_trigger_agent(session, has_agent_mention):
        return JSONResponse(
            {"status": "accepted", "message_id": str(new_message_id), "agent_triggered": False},
            status_code=202,
        )

    # --- Trigger agent (with coalescing) ---
    chosen_model = (body.model or "").strip() or getattr(session, "model", None)
    if not chosen_model:
        raise HTTPException(status_code=400, detail="model is required.")
    try:
        resolved = await resolve_model(db, chosen_model)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if resolved["model"] != getattr(session, "model", None):
        await update_session_model(db, session_id, resolved["model"])

    history = await get_thread_messages_as_conversation(db, session_id, root_id)
    loop = asyncio.get_running_loop()
    feature_id = getattr(session, "feature_id", "") or ""

    await schedule_agent_turn(
        session_id=session_id,
        message=body.content,
        history=history,
        workspace_id=ws_id,
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
        reply_to_message_id=inner_reply_to_id,
        thread_root_id=root_id,
        image_ids=body.image_ids,
    )

    return JSONResponse(
        {
            "status": "accepted",
            "message_id": str(new_message_id),
            "agent_triggered": True,
            "agent_mentions": [
                m for m in resolved_mentions if m["mentioned_kind"] == "agent"
            ],
        },
        status_code=202,
    )


@router.get("/threads/{session_id}/messages/{message_id}/replies")
async def get_message_thread_replies(
    session_id: str,
    message_id: str,
    since: str = Query("", description="Return only replies after this message id (cursor)"),
    _identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return thread replies for the thread rooted at message_id, oldest-first.

    Supports the same ?since= cursor used by the main transcript for SSE catch-up.
    Replies are author-enriched via attach_authors (same helper as the main transcript).
    """
    try:
        root_id = int(message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="message_id must be a numeric id.")

    since_id: int | None = None
    if since:
        try:
            since_id = int(since)
        except ValueError:
            since_id = None

    # Verify the session exists (and load workspace_id for author resolution).
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Thread not found.")

    replies = await get_thread_replies(db, session_id, root_id, since=since_id)

    workspace_id = getattr(session, "workspace_id", "") or "" if session else ""
    await attach_authors(workspace_id, replies)
    for r in replies:
        image_ids = r.pop("image_ids", None)
        if image_ids:
            r["image_urls"] = _image_urls_for(workspace_id, image_ids)

    return JSONResponse({"replies": replies})
