"""Workspace-level team thread routes (T9) + cancel endpoint (m3-stop-agent-chat T1).

Routes (all under ``/api/v1``, require_identity):

    POST /threads                           — create a workspace-level thread
    GET  /threads?workspace_id=<ws>         — list caller's workspace threads
    GET  /sessions?workspace_id=<ws>        — (existing, extended) now returns
                                              feature threads + workspace threads
    POST /threads/{session_id}/cancel       — cancel an in-progress agent turn

A workspace thread is a ``kind='thread'``, ``feature_id=''`` session with explicit
membership (unlike a public channel). The full T2/T3 conversation stack
(send, SSE fan-out, @agent dispatch) applies unchanged — these routes only
handle creation and listing.
"""

from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.agent_dispatch import (
    _active_runs,
    _active_runs_lock,
)
from src.api.deps import get_db
from src.api.identity import Identity, require_identity
from src.db import (
    create_workspace_thread,
    list_workspace_threads,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class CreateThreadRequest(BaseModel):
    workspace_id: str
    title: Optional[str] = None
    members: Optional[List[str]] = None


class ThreadResponse(BaseModel):
    thread_id: str


class ClarifyReplyRequest(BaseModel):
    clarify_id: str
    response: str = ""


# ---------------------------------------------------------------------------
# POST /threads
# ---------------------------------------------------------------------------


@router.post("/threads", status_code=201)
async def create_thread_endpoint(
    body: CreateThreadRequest,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Create a workspace-level team thread.

    The caller is auto-joined as creator. Optional *members* list adds extra
    members at creation time. Returns 400 if workspace_id is missing.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")
    if not body.workspace_id:
        raise HTTPException(status_code=400, detail="workspace_id is required.")

    thread_id = await create_workspace_thread(
        db,
        workspace_id=body.workspace_id,
        creator_user_id=user_id,
        title=body.title,
        members=body.members,
    )

    logger.info(
        "Workspace thread created: %s (workspace=%s, creator=%s, members=%s)",
        thread_id,
        body.workspace_id,
        user_id,
        body.members,
    )
    return JSONResponse({"thread_id": thread_id}, status_code=201)


# ---------------------------------------------------------------------------
# GET /threads
# ---------------------------------------------------------------------------


@router.get("/threads")
async def list_threads_endpoint(
    workspace_id: str = Query(..., description="Workspace slug or ID"),
    limit: int = Query(50, ge=1, le=200, description="Max threads to return"),
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Return workspace-level threads the caller owns or is a member of.

    Excludes feature threads (feature_id != '') and channels (kind='channel').
    Non-members are excluded — only own ∪ member-of threads are returned.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    threads = await list_workspace_threads(
        db,
        workspace_id=workspace_id,
        user_id=user_id,
        limit=limit,
    )
    return JSONResponse({"threads": threads})


# ---------------------------------------------------------------------------
# POST /threads/{session_id}/cancel
# ---------------------------------------------------------------------------


@router.post("/threads/{session_id}/cancel", status_code=202)
async def cancel_agent_turn(
    session_id: str,
    identity: Identity = Depends(require_identity),
) -> JSONResponse:
    """Cancel an in-progress agent turn for the given thread/session.

    Returns 202 immediately — cancellation is asynchronous.
    Returns 404 if no agent turn is currently running for this session.
    Returns 403 if the caller is not the member who triggered the turn.

    Cancellation has three parts that must all fire — task.cancel() alone leaves
    the blocking worker thread running (run_in_executor threads aren't cancellable),
    which is why a cancelled turn used to finish and persist its reply anyway:
      1. cancel_event — tells the worker thread to suppress DB mirror writes and
         skip the backfill, so nothing produced after Stop is persisted.
      2. agent.interrupt() — aborts the in-flight LLM call at the socket level and
         stops the agent's tool loop so generation actually halts.
      3. task.cancel() — the async Task receives CancelledError at its next await,
         flushes any accumulated partial tokens to the DB with
         finish_reason='stopped', and publishes turn.stopped to all subscribers.
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    with _active_runs_lock:
        active_run = _active_runs.get(session_id)

    if active_run is None or active_run.task is None:
        raise HTTPException(status_code=404, detail="no_active_turn")

    if active_run.triggered_by != user_id:
        raise HTTPException(status_code=403, detail="not_triggering_member")

    # 1. Flag the worker thread before anything else so it stops persisting.
    active_run.cancel_event.set()

    # 1b. Unblock a pending `clarify` wait — interrupt()/task.cancel() below
    #     only reach the LLM call and the async Task; a worker thread parked
    #     in clarify_gateway.wait_for_response() is otherwise deaf to both and
    #     would keep the active-run guard pinned for up to the full timeout.
    try:
        from tools import clarify_gateway

        clarify_gateway.clear_session(session_id)
    except Exception:
        logger.exception(
            "threads: failed to clear pending clarify for session %s", session_id
        )

    # 2. Interrupt the live agent so the in-flight LLM call/tool loop unwinds.
    #    May be None if cancel races setup before the agent is built — the
    #    cancel_event still makes the worker bail at its next checkpoint.
    if active_run.agent is not None:
        try:
            active_run.agent.interrupt()
        except Exception:
            logger.exception(
                "threads: agent.interrupt() failed for session %s", session_id
            )

    # 3. Cancel the async task — drives the turn.stopped event and partial flush.
    active_run.task.cancel()
    logger.info(
        "threads: cancel requested for session %s by user %s", session_id, user_id
    )
    return JSONResponse({"status": "cancelling"}, status_code=202)


# ---------------------------------------------------------------------------
# POST /threads/{session_id}/clarify
# ---------------------------------------------------------------------------


@router.post("/threads/{session_id}/clarify", status_code=202)
async def reply_to_clarify(
    session_id: str,
    body: ClarifyReplyRequest,
    identity: Identity = Depends(require_identity),
) -> JSONResponse:
    """Answer a pending ``agent.clarify`` prompt, unblocking the agent's turn.

    The turn's worker thread is parked inside the `clarify` tool waiting on
    ``clarify_id`` (see ``agent_dispatch._make_clarify_callback``); this just
    resolves that wait — it does not itself touch the SSE/bus stream, which
    resumes automatically once the agent's tool call returns.

    Returns 404 if there's no active turn, or if ``clarify_id`` doesn't match
    any pending entry (already resolved, expired, or never existed — e.g. a
    stale UI prompt from a previous turn).
    """
    user_id = identity.user_id
    if not user_id:
        raise HTTPException(status_code=400, detail="Missing caller identity.")

    with _active_runs_lock:
        active_run = _active_runs.get(session_id)

    if active_run is None or active_run.task is None:
        raise HTTPException(status_code=404, detail="no_active_turn")

    if active_run.triggered_by != user_id:
        raise HTTPException(status_code=403, detail="not_triggering_member")

    from tools import clarify_gateway

    resolved = clarify_gateway.resolve_gateway_clarify(body.clarify_id, body.response)
    if not resolved:
        raise HTTPException(status_code=404, detail="clarify_not_pending")

    return JSONResponse({"status": "accepted"}, status_code=202)
