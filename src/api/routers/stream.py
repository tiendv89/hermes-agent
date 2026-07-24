"""SSE stream endpoint + typing indicator for real-time thread delivery (T3).

Routes (all under ``/api/v1``, require_identity):

    GET  /threads/{session_id}/stream          — persistent SSE subscription
    POST /threads/{session_id}/typing          — ephemeral typing indicator

Design (§4.3):
- Each ``GET .../stream`` caller subscribes to the in-process bus for the
  thread topic.  The bus fan-out delivers: ``message.created`` (from the send
  service), agent delta frames + progress events (from BusPublishingSSETranslator),
  ``typing``/``agent.working`` (ephemeral), ``member.changed``,
  ``channel.deleted``, ``message.thread_updated`` (a root message's refreshed
  reply_count/recent_repliers, published by agent_dispatch right after a
  threaded reply is persisted — the live equivalent of the thread_summary a
  reload attaches via GET .../messages).
- ``?since=<message_id>`` replays persisted messages with id > since before
  switching to live tailing; the subscription is registered *before* the DB
  replay to ensure no live events are missed during the catch-up window.
- ``POST .../typing`` publishes an ephemeral event to the bus without persisting
  anything.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.api.thread_authz import authorize_thread_access
from src.db import (
    add_member,
    get_messages_since,
    get_session,
)
from src.realtime.bus import get_bus
from src.services.author_resolver import attach_authors

logger = logging.getLogger(__name__)

router = APIRouter()

# How long to wait for a bus event before emitting a keepalive comment.
_KEEPALIVE_SECONDS = 20.0


def _sse_frame(event: str, data: dict) -> str:
    """Render a named SSE frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# ---------------------------------------------------------------------------
# GET /threads/{session_id}/stream
# ---------------------------------------------------------------------------


@router.get("/threads/{session_id}/stream")
async def stream_thread(
    session_id: str,
    since: str | None = Query(
        default=None, description="Replay messages with id > since"
    ),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """Open a persistent SSE subscription for a thread or channel.

    Delivers all bus events for the thread: persisted messages (``message.created``),
    agent delta / tool / artifact frames, ephemeral ``typing``/``agent.working``,
    ``member.changed``, and ``channel.deleted``.

    ``?since=<message_id>`` causes a replay of missed persisted messages before
    live tailing starts; the subscription is registered before the replay query
    so no in-flight event is missed.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Thread not found.")

    # Sessions (kind='thread', feature-scoped or workspace-level) are org-public
    # like channels — any org member is authorized to view even without an
    # explicit session_members row.
    caller_is_workspace_member, _org_id = await authorize_thread_access(
        db, session, user_id, identity.org_id
    )
    kind_val = getattr(session, "kind", "thread") or "thread"

    # Implicit join for authorized org members on any thread session (feature-
    # scoped or workspace-level) — idempotent (add_member is a no-op when the
    # row already exists).
    if kind_val == "thread" and caller_is_workspace_member:
        await add_member(db, session_id, user_id, added_by=user_id)

    # Parse the since cursor (treat as message integer id; ignore if non-numeric).
    since_id: int | None = None
    if since:
        try:
            since_id = int(since)
        except ValueError:
            pass

    # Subscribe to the bus BEFORE the DB replay fetch (§4.3).
    # Registering the queue here — while still in the request handler — ensures
    # no live event published between the DB query and the generator starting
    # is lost. The generator owns cleanup via finally.
    bus = get_bus()
    queue: asyncio.Queue = asyncio.Queue(maxsize=256)
    bus.subscribe_raw(session_id, queue)

    replay_messages: list = []
    if since_id is not None:
        replay_messages = await get_messages_since(db, session_id, since_id)
        # Enrich author display info so replayed messages show real names.
        await attach_authors(
            getattr(session, "workspace_id", "") or "", replay_messages
        )

    async def event_generator():
        try:
            # Replay missed persisted messages first; subscription is already
            # active so no in-flight live events are lost during this window.
            for msg in replay_messages:
                yield _sse_frame("message.created", msg)
                await asyncio.sleep(0)  # flush write buffer between replayed frames

            # Tail the live stream indefinitely.
            while True:
                try:
                    bus_event = await asyncio.wait_for(queue.get(), _KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield ": keepalive\n\n"
                    continue

                event_name = bus_event.get("event", "")
                data = bus_event.get("data", {})

                if not event_name:
                    continue

                yield _sse_frame(event_name, data)
                # Yield control back to the event loop so uvicorn flushes the
                # write buffer before processing the next queued event. Without
                # this, multiple frames that arrive in a burst (e.g. rapid agent
                # deltas) are all written in the same asyncio cycle and land in
                # one TCP segment, making the UI render text in large chunks.
                await asyncio.sleep(0)

                # Close the stream after the channel hosting this session is
                # hard-deleted so the client knows to navigate away.
                if (
                    event_name == "channel.deleted"
                    and data.get("session_id") == session_id
                ):
                    return
        finally:
            bus.unsubscribe_raw(session_id, queue)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ---------------------------------------------------------------------------
# POST /threads/{session_id}/typing
# ---------------------------------------------------------------------------


@router.post("/threads/{session_id}/typing", status_code=204)
async def post_typing(
    session_id: str,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Publish an ephemeral typing indicator for the caller.

    Not persisted — subscribers see it on their ``…/stream`` as a ``typing``
    event; it disappears when they reconnect.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    session = await get_session(db, session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Thread not found.")

    await authorize_thread_access(db, session, user_id, identity.org_id)

    get_bus().publish(
        session_id,
        {"event": "typing", "data": {"user_id": user_id, "session_id": session_id}},
    )
