"""Send service — POST /api/v1/threads/{session_id}/messages (v4 team-chat).

Decouples human-message persistence from the agent turn:
  1. Persist the human message with author_id.
  2. Parse + resolve @mentions; persist message_mentions.
  3. Gate agent dispatch per trigger rules (§4.2):
       - Explicit @agent mention → trigger.
       - Thread (feature-scoped or ad-hoc) + bare message (no @agent) → trigger (v3 feel preserved).
       - Channel or DM + bare message → no trigger.
  4. If triggered: schedule an agent turn with coalescing (via agent_dispatch).
  5. Return 202 immediately (fire-and-forget pattern).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from src.api.agent_dispatch import schedule_agent_turn
from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.api.mentions import parse_mention_handles, resolve_mentions
from src.api.model_catalog import default_model, resolve_model
from src.db import (
    edit_message,
    get_message,
    get_messages_as_conversation,
    get_messages_since,
    get_session,
    get_session_messages,
    is_member,
    persist_mentions,
    set_session_title,
    soft_delete_message,
    touch_session,
    update_session_model,
)
from src.db.store import append_message, get_thread_reply_summaries
from src.realtime.bus import get_bus
from src.services.author_resolver import attach_authors, author_for, mention_candidates
from src.services.workflow_backend_client import get_workspace_organization_id

logger = logging.getLogger(__name__)

router = APIRouter()


class EditMessageRequest(BaseModel):
    content: str


class SendMessageRequest(BaseModel):
    content: str
    # Optional model override (same semantics as legacy /chat).
    model: str = ""
    # Optional: ID of the message this is a direct reply to (G1 inline reply).
    # When set, reply_to_message_id is stored on the persisted message.
    # thread_root_id remains NULL — the reply stays in the main transcript.
    reply_to_message_id: Optional[str] = None
    # IDs of images uploaded to storage-service's images bucket (see
    # storage_service_client.download_image) that the user pasted/attached to
    # this message. hermes-agent downloads them server-side and hands the
    # agent a local file path — see agent_dispatch.py's image handling —
    # rather than a URL, since storage-service is internal-only and the
    # vision tool's SSRF guard would reject fetching it directly.
    image_ids: List[str] = []


def _image_urls_for(workspace_id: str, image_ids) -> List[str]:
    """Bare storage-service image ids -> BFF-relative fetch URLs.

    Stored as bare ids (see storage_service_client.download_image, used
    server-side by the agent's vision tool); resolved to URLs here, where the
    session's workspace_id is in scope, so the frontend just needs to prefix
    with the BFF base (see imageSrcUrl in storage-service/images.ts).

    Defensively re-parses a JSON-encoded string: a since-fixed bug in
    append_message double-encoded the JSONB image_ids column (json.dumps'd a
    list before assigning it to a column whose dialect already serializes
    JSON on write), so old rows can still come back as a string like
    '["<uuid>"]' instead of a real list. Iterating that string directly
    walks it character-by-character, producing one bogus one-char "image" per
    character — this guard makes such legacy rows render correctly instead.
    """
    if isinstance(image_ids, str):
        try:
            image_ids = json.loads(image_ids)
        except (ValueError, TypeError):
            image_ids = []
    if not isinstance(image_ids, list):
        return []
    return [f"/api/workspaces/{workspace_id}/images/{image_id}" for image_id in image_ids]


def _attach_image_urls(messages: list, workspace_id: str) -> None:
    """Replace each message's raw `image_ids` with resolved `image_urls`, in place."""
    for m in messages:
        image_ids = m.pop("image_ids", None)
        if image_ids:
            m["image_urls"] = _image_urls_for(workspace_id, image_ids)


def _should_trigger_agent(session, has_explicit_agent_mention: bool) -> bool:
    """Dispatch gate: decide whether this message should start an agent turn.

    Rules (§4.2, resolved):
      - Explicit @agent mention → always trigger.
      - Bare message in any thread (kind='thread', feature-scoped or ad-hoc) → trigger (v3 feel).
      - Bare message in a channel (kind='channel') → never trigger.
      - Bare message in a DM (kind='dm') → never trigger (same as channel rule; a
        1:1 private exchange must not have every message intercepted by the agent).
    """
    if has_explicit_agent_mention:
        return True
    kind = getattr(session, "kind", "thread") or "thread"
    return kind == "thread"


def _agent_reply_thread_context(session, message) -> tuple:
    """Return (thread_root_id, reply_to_message_id) to pass to schedule_agent_turn.

    Determines where the agent's reply should be persisted given the triggering message:
    - G1: mention inside an existing thread → passthrough the thread context unchanged.
    - G2: channel/DM top-level mention → auto-open a thread rooted at the triggering message.
    - G3: feature thread top-level mention → flat reply (unchanged behavior).
    """
    if message.thread_root_id is not None:
        # G1: already inside a message thread — unchanged passthrough.
        return message.thread_root_id, message.id
    if getattr(session, "kind", "thread") in ("channel", "dm"):
        # G2: channel/DM, top-level mention — auto-open a thread rooted at
        # the triggering message.
        return message.id, message.id
    # G3: thread (feature-scoped or ad-hoc), top-level mention — unchanged flat reply.
    return None, None


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
    _attach_image_urls(messages, workspace_id)

    # Attach thread_summary to each top-level message (reply count + recent repliers).
    if messages:
        root_ids = [int(m["id"]) for m in messages]
        summaries = await get_thread_reply_summaries(db, session_id, root_ids)
        for m in messages:
            mid = int(m["id"])
            if mid in summaries:
                m["thread_summary"] = summaries[mid]

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
    # Empty text is fine for an image-only send — must have at least one or the other.
    if (not body.content or not body.content.strip()) and not body.image_ids:
        raise HTTPException(status_code=400, detail="content or image_ids must be non-empty.")

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
    try:
        org_id = await get_workspace_organization_id(
            ws_id, user_id=identity.user_id, org_id=identity.org_id
        ) or ""
    except Exception:
        logger.exception("workflow-backend org_id lookup failed for workspace %s", ws_id)
        org_id = ""

    # --- Mention parse + resolve ---
    handles = parse_mention_handles(body.content)
    has_agent_mention = "agent" in handles

    # Resolve @handles against the whole org directory (user-service), so any
    # org member can be mentioned — not only current channel members.
    resolved_mentions = resolve_mentions(handles, await mention_candidates(org_id))

    # --- Persist the human message (with author_id) ---
    reply_to_id: Optional[int] = None
    if body.reply_to_message_id:
        try:
            reply_to_id = int(body.reply_to_message_id)
        except ValueError:
            raise HTTPException(status_code=400, detail="reply_to_message_id must be a numeric id.")

    message_id = await append_message(
        db,
        session_id=session_id,
        role="user",
        content=body.content,
        author_id=user_id,
        reply_to_message_id=reply_to_id,
        image_ids=body.image_ids,
    )

    # --- Persist resolved mentions ---
    if resolved_mentions:
        await persist_mentions(
            db,
            message_id=message_id,
            session_id=session_id,
            mentions=resolved_mentions,
            content=body.content,
            author_id=user_id,
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
                "reply_to_message_id": str(reply_to_id) if reply_to_id is not None else None,
                "thread_root_id": None,
                "image_urls": _image_urls_for(ws_id, body.image_ids) if body.image_ids else [],
            },
        },
    )

    # --- Dispatch gate ---
    if not _should_trigger_agent(session, has_agent_mention):
        return JSONResponse(
            {"status": "accepted", "message_id": str(message_id), "agent_triggered": False},
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

    _trigger_msg = SimpleNamespace(id=message_id, thread_root_id=None)
    thread_root_id, agent_reply_to_id = _agent_reply_thread_context(session, _trigger_msg)

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
        image_ids=body.image_ids,
        reply_to_message_id=agent_reply_to_id,
        thread_root_id=thread_root_id,
    )

    return JSONResponse(
        {
            "status": "accepted",
            "message_id": str(message_id),
            "agent_triggered": True,
            "agent_mentions": [
                m for m in resolved_mentions if m["mentioned_kind"] == "agent"
            ],
        },
        status_code=202,
    )


@router.put("/messages/{message_id}")
async def edit_message_endpoint(
    message_id: str,
    body: EditMessageRequest,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Edit a message's content (author only).

    Sets ``edited_at`` to the current time and returns the updated message.
    Only the original author (X-User-Id == message.author_id) may call this.
    """
    if not body.content or not body.content.strip():
        raise HTTPException(status_code=400, detail="content must be non-empty.")
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    try:
        msg_id = int(message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="message_id must be numeric.")

    msg = await get_message(db, msg_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found.")
    if msg.author_id != user_id:
        raise HTTPException(status_code=403, detail="Only the author can edit this message.")

    await edit_message(db, msg_id, body.content)
    updated = await get_message(db, msg_id)
    return JSONResponse(
        {
            "id": str(updated.id),
            "session_id": updated.session_id,
            "content": updated.content,
            "author_id": updated.author_id,
            "created_at": updated.created_at,
            "edited_at": updated.edited_at,
            "active": updated.active,
        }
    )


@router.delete("/messages/{message_id}")
async def delete_message_endpoint(
    message_id: str,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Soft-delete a message (author only).

    Sets ``active=False`` — the message is not removed from the DB so thread/reply
    linkage is preserved. The read path renders inactive rows as
    "This message was deleted" placeholders. Idempotent: calling again on an
    already-deleted message is a no-op.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    try:
        msg_id = int(message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="message_id must be numeric.")

    msg = await get_message(db, msg_id)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found.")
    if msg.author_id != user_id:
        raise HTTPException(status_code=403, detail="Only the author can delete this message.")

    await soft_delete_message(db, msg_id)
    return JSONResponse({"ok": True, "message_id": message_id})
