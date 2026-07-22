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
import time as _time
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import List, Optional

from src.db.models import Message as MessageModel

from src.api.agent_dispatch import schedule_agent_turn, try_resolve_pending_clarify
from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.api.mentions import parse_mention_handles, resolve_mentions
from src.api.model_catalog import resolve_model
from src.api.thread_authz import authorize_thread_access
from src.db import (
    add_member,
    edit_message,
    get_message,
    get_messages_as_conversation,
    get_messages_since,
    get_session,
    get_session_messages,
    persist_mentions,
    set_session_title,
    soft_delete_message,
    toggle_message_reaction,
    touch_session,
    update_session_model,
)
from src.db.models import Message, Session
from src.db.store import append_message, get_thread_reply_summaries
from src.realtime.bus import get_bus
from src.services.author_resolver import attach_authors, author_for, mention_candidates
from src.services.user_service_client import list_users_by_ids

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

    # IDs of files uploaded to storage-service's files bucket (see
    # storage_service_client.download_file) that the user attached to this
    # message. Handled alongside image_ids through the entire message
    # pipeline — persisted, forwarded to the agent turn, and rendered
    # as file chips in the frontend.
    file_ids: List[str] = []


class ForwardMessageRequest(BaseModel):
    destination_session_ids: List[str]
    # Optional comment prepended to the forwarded content as:
    # "<comment>\n\n<original content>" in a single message row.
    comment: Optional[str] = None


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
    return [
        f"/api/workspaces/{workspace_id}/images/{image_id}" for image_id in image_ids
    ]


def _attach_image_urls(messages: list, workspace_id: str) -> None:
    """Replace each message's raw `image_ids` with resolved `image_urls`, in place."""
    for m in messages:
        image_ids = m.pop("image_ids", None)
        if image_ids:
            m["image_urls"] = _image_urls_for(workspace_id, image_ids)


def _file_urls_for(workspace_id: str, file_ids) -> List[str]:
    """Bare storage-service file ids -> BFF-relative fetch URLs.

    Mirrors _image_urls_for but constructs /files/ URLs instead of /images/.
    Same defensive re-parse logic for legacy double-encoded rows.
    """
    if isinstance(file_ids, str):
        try:
            file_ids = json.loads(file_ids)
        except (ValueError, TypeError):
            file_ids = []
    if not isinstance(file_ids, list):
        return []
    return [
        f"/api/workspaces/{workspace_id}/files/{file_id}" for file_id in file_ids
    ]


def _attach_file_urls(messages: list, workspace_id: str) -> None:
    """Replace each message's raw `file_ids` with resolved `file_urls`, in place."""
    for m in messages:
        file_ids = m.pop("file_ids", None)
        if file_ids:
            m["file_urls"] = _file_urls_for(workspace_id, file_ids)


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
    since: str = Query(
        "", description="Return only messages after this message id (cursor)"
    ),
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
        messages = await get_session_messages(db, session_id, user_id=_identity.user_id)

    # Enrich author display info (name/avatar) from user-service so the channel
    # transcript shows real names rather than raw ids.
    session = await get_session(db, session_id)
    workspace_id = getattr(session, "workspace_id", "") or "" if session else ""
    await attach_authors(workspace_id, messages)
    _attach_image_urls(messages, workspace_id)
    _attach_file_urls(messages, workspace_id)
    await _attach_forwarded_authors(messages, db)
    await _attach_reaction_users(
        [m["reactions"] for m in messages if m.get("reactions")]
    )

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
    # Empty text is fine for an image/file-only send — must have at least one or the other.
    if (not body.content or not body.content.strip()) and not body.image_ids and not body.file_ids:
        raise HTTPException(
            status_code=400, detail="content, image_ids, or file_ids must be non-empty."
        )

    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    # Verify the session exists.
    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Thread not found.")

    ws_id = getattr(session, "workspace_id", "") or ""

    # Sessions (kind='thread', feature-scoped or workspace-level) are org-public
    # like channels — any org member is authorized to post even without an
    # explicit session_members row. org_id doubles as workspace/org context for
    # agent dispatch and @mention resolution below.
    caller_is_workspace_member, org_id = await authorize_thread_access(
        db, session, identity.user_id, identity.org_id
    )
    kind_val = getattr(session, "kind", "thread") or "thread"

    # Implicit join for authorized org members on any thread session (feature-
    # scoped or workspace-level) — idempotent.
    if kind_val == "thread" and caller_is_workspace_member:
        await add_member(db, session_id, user_id, added_by=user_id)

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
            raise HTTPException(
                status_code=400, detail="reply_to_message_id must be a numeric id."
            )

    message_id = await append_message(
        db,
        session_id=session_id,
        role="user",
        content=body.content,
        author_id=user_id,
        reply_to_message_id=reply_to_id,
        image_ids=body.image_ids,
        file_ids=body.file_ids,
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
        first_line = (
            body.content.strip().splitlines()[0] if body.content.strip() else ""
        )
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
                "reply_to_message_id": str(reply_to_id)
                if reply_to_id is not None
                else None,
                "thread_root_id": None,
                "image_urls": _image_urls_for(ws_id, body.image_ids)
                if body.image_ids
                else [],
                "file_urls": _file_urls_for(ws_id, body.file_ids)
                if body.file_ids
                else [],
            },
        },
    )

    # --- Clarify resolution ---
    # If the triggering user's turn is parked waiting on a `clarify` prompt,
    # this message IS the answer — resolve it directly instead of letting the
    # dispatch gate below coalesce it behind the still-in-flight turn (where
    # it would sit unseen for up to the clarify timeout). See
    # agent_dispatch.try_resolve_pending_clarify.
    if try_resolve_pending_clarify(session_id, user_id, body.content):
        return JSONResponse(
            {
                "status": "accepted",
                "message_id": str(message_id),
                "agent_triggered": False,
            },
            status_code=202,
        )

    # --- Dispatch gate ---
    if not _should_trigger_agent(session, has_agent_mention):
        return JSONResponse(
            {
                "status": "accepted",
                "message_id": str(message_id),
                "agent_triggered": False,
            },
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

    # Load conversation history (which now includes the pre-persisted user message).
    history = await get_messages_as_conversation(db, session_id)

    loop = asyncio.get_running_loop()
    workspace_id = ws_id
    feature_id = getattr(session, "feature_id", "") or ""

    _trigger_msg = SimpleNamespace(id=message_id, thread_root_id=None)
    thread_root_id, agent_reply_to_id = _agent_reply_thread_context(
        session, _trigger_msg
    )

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
        file_ids=body.file_ids,
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


async def _attach_forwarded_authors(messages: list, db: AsyncSession) -> None:
    """Batch-resolve forwarded_from_message_id → original author info.

    For each message with ``forwarded_from_message_id`` set, looks up the
    original message's ``author_id`` in one batched SQL query, then resolves
    display info via user-service in a single batched HTTP call.  Results are
    attached in-place as a ``forwarded_from`` dict:
    ``{"id": str, "name": str|None, "avatarUrl": str|None}``.
    """
    forwarded_ids = [
        int(m["forwarded_from_message_id"])
        for m in messages
        if m.get("forwarded_from_message_id") is not None
    ]
    if not forwarded_ids:
        return

    result = await db.execute(
        select(Message.id, Message.author_id).where(Message.id.in_(forwarded_ids))
    )
    originals: dict = {str(row.id): row.author_id for row in result.all()}

    author_ids = list({aid for aid in originals.values() if aid})
    if not author_ids:
        return

    users = await list_users_by_ids(author_ids)

    for m in messages:
        fwd_id = m.get("forwarded_from_message_id")
        if fwd_id is None:
            continue
        orig_author_id = originals.get(fwd_id)
        if not orig_author_id:
            continue
        info = users.get(orig_author_id) or {}
        name = (info.get("display_name") or "").strip() or None
        if not name:
            email = (info.get("email") or "").strip()
            name = email.split("@")[0] if email else None
        m["forwarded_from"] = {
            "id": orig_author_id,
            "name": name,
            "avatarUrl": info.get("avatar_url") or None,
        }


async def _attach_reaction_users(reaction_lists: list) -> None:
    """Batch-resolve each reaction group's ``userIds`` to display names.

    ``reaction_lists`` is a list of a message's ``reactions`` arrays (each entry has
    ``userIds`` from :func:`get_reactions_for_messages` / :func:`toggle_message_reaction`).
    Replaces ``userIds`` with a resolved ``users: [{id, name}]`` list in place, powering
    a "Name1, Name2 reacted with :emoji:" hover tooltip client-side.
    """
    all_ids = {
        uid
        for reactions in reaction_lists
        for r in reactions
        for uid in r.get("userIds", [])
    }
    users = await list_users_by_ids(list(all_ids)) if all_ids else {}

    def _name_for(uid: str) -> str:
        info = users.get(uid) or {}
        name = (info.get("display_name") or "").strip()
        if not name:
            email = (info.get("email") or "").strip()
            name = email.split("@")[0] if email else uid
        return name

    for reactions in reaction_lists:
        for r in reactions:
            uids = r.pop("userIds", [])
            r["users"] = [{"id": uid, "name": _name_for(uid)} for uid in uids]


@router.post("/messages/{message_id}/forward", status_code=201)
async def forward_message(
    message_id: str,
    body: ForwardMessageRequest,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Forward a message to one or more destination sessions.

    Creates one new ``Message`` row per destination with
    ``forwarded_from_message_id`` pointing at the source.  If ``comment`` is
    provided it is prepended to the forwarded content as
    ``"<comment>\\n\\n<original content>"`` in a single row — no separate row
    is created for the comment.  This design keeps the forwarded message atomic
    and avoids an extra N insert per destination.

    Auth: any identified caller (no ownership check on the source message — any
    member of a session can forward a message from it, matching the spec's
    intent).  The caller's ``user_id`` becomes the ``author_id`` of the
    forwarded copies.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    if not body.destination_session_ids:
        raise HTTPException(
            status_code=400, detail="destination_session_ids must be non-empty."
        )

    try:
        src_id = int(message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="message_id must be numeric.")

    result = await db.execute(
        select(Message).where(Message.id == src_id, Message.active == True)  # noqa: E712
    )
    source_msg = result.scalar_one_or_none()
    if source_msg is None:
        raise HTTPException(status_code=404, detail="Message not found.")

    forwarded_content = source_msg.content or ""
    if body.comment:
        forwarded_content = f"{body.comment.strip()}\n\n{forwarded_content}"

    # Pre-validate all destinations before writing any messages to ensure atomicity.
    # Use a single IN-query instead of N individual get_session calls to avoid N+1.
    found_sessions_result = await db.execute(
        select(Session).where(Session.id.in_(body.destination_session_ids))
    )
    sessions_by_id = {s.id: s for s in found_sessions_result.scalars().all()}
    for dest_session_id in body.destination_session_ids:
        if dest_session_id not in sessions_by_id:
            raise HTTPException(
                status_code=404,
                detail=f"Destination session not found: {dest_session_id}",
            )

    # Resolve display info once — same for every destination — for the SSE fan-out below.
    forwarder_author = await author_for("", user_id)
    original_author = (
        await author_for("", source_msg.author_id) if source_msg.author_id else None
    )

    # All destinations valid — safe to write
    new_message_ids = []
    for dest_session_id in body.destination_session_ids:
        new_id = await append_message(
            db,
            session_id=dest_session_id,
            role="user",
            content=forwarded_content,
            author_id=user_id,
            forwarded_from_message_id=src_id,
        )
        new_message_ids.append(new_id)

        # Live fan-out so other subscribers (and the sender, on other tabs) see the
        # forwarded message land without needing a reload — mirrors send_message's publish.
        get_bus().publish(
            dest_session_id,
            {
                "event": "message.created",
                "data": {
                    "id": str(new_id),
                    "session_id": dest_session_id,
                    "role": "user",
                    "content": forwarded_content,
                    "author_id": user_id,
                    "author": forwarder_author,
                    "created_at": _time.time(),
                    "forwarded_from_message_id": str(src_id),
                    "forwarded_from": original_author,
                },
            },
        )

    return JSONResponse(
        {
            "forwarded_message_ids": new_message_ids,
            "destination_session_ids": body.destination_session_ids,
        },
        status_code=201,
    )


# ---------------------------------------------------------------------------
# Reaction endpoint
# ---------------------------------------------------------------------------


class ToggleReactionRequest(BaseModel):
    emoji: str


@router.post("/messages/{message_id}/reactions")
async def toggle_reaction(
    message_id: str,
    body: ToggleReactionRequest,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Toggle an emoji reaction on a message.

    Adds the reaction if the calling user hasn't reacted with this emoji yet;
    removes it if they have. Returns the updated aggregate reaction list for
    the message: ``[{emoji, count, reactedByMe}]``.
    """
    if not body.emoji or not body.emoji.strip():
        raise HTTPException(status_code=400, detail="emoji must be non-empty.")

    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    try:
        msg_id = int(message_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="message_id must be numeric.")

    # Verify message exists and fetch its session for membership check.
    result = await db.execute(
        select(MessageModel.id, MessageModel.session_id).where(
            MessageModel.id == msg_id
        )
    )
    msg_row = result.one_or_none()
    if msg_row is None:
        raise HTTPException(status_code=404, detail="Message not found.")

    session = await get_session(db, msg_row.session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found.")

    await authorize_thread_access(db, session, identity.user_id, identity.org_id)

    reactions = await toggle_message_reaction(db, msg_id, user_id, body.emoji.strip())
    await _attach_reaction_users([reactions])

    # Live fan-out so every other subscriber sees the updated reaction list without a
    # reload. `reactedByMe` here reflects the toggling caller — other viewers re-derive
    # their own reactedByMe client-side from each group's `users` list.
    get_bus().publish(
        msg_row.session_id,
        {
            "event": "message.reactions_updated",
            "data": {"message_id": message_id, "reactions": reactions},
        },
    )

    return JSONResponse({"reactions": reactions})


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
        raise HTTPException(
            status_code=403, detail="Only the author can edit this message."
        )

    await edit_message(db, msg_id, body.content)
    updated = await get_message(db, msg_id)

    get_bus().publish(
        updated.session_id,
        {
            "event": "message.edited",
            "data": {
                "message_id": str(updated.id),
                "content": updated.content,
                "edited_at": updated.edited_at,
            },
        },
    )

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
        raise HTTPException(
            status_code=403, detail="Only the author can delete this message."
        )

    await soft_delete_message(db, msg_id)

    get_bus().publish(
        msg.session_id,
        {"event": "message.deleted", "data": {"message_id": message_id}},
    )

    return JSONResponse({"ok": True, "message_id": message_id})
