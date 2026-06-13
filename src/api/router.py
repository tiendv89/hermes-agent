"""FastAPI router for the workflow gateway.

Routes:
    POST /session — create a new session
    GET /sessions — list sessions for a workspace+feature
    GET /sessions/{session_id}/messages — load a session's transcript
    POST /chat — run one agent turn and stream SSE back
    PUT /features/{feature_id}/document — human-save a document to the feature branch
    GET /tools — live tool registry (for the FE slash-command picker)
    POST /features/{feature_id}/stage-transition — ts lifecycle write (approve/reject/reopen)

The router is mounted at ``/api/v1`` in ``src/app.py``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import AsyncIterator, Optional, Set

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


# ---------------------------------------------------------------------------
# PUT /features/{feature_id}/document — human-save endpoint
# ---------------------------------------------------------------------------


class SaveDocumentRequest(BaseModel):
    doc: str  # "product_spec" or "technical_design"
    content: str
    base_sha: Optional[str] = None  # None for new files


@router.put("/features/{feature_id}/document")
async def save_document_endpoint(
    feature_id: str,
    body: SaveDocumentRequest,
    identity: Identity = Depends(require_identity),
) -> JSONResponse:
    """Commit a human-edited document to the feature branch.

    The ``base_sha`` comes from workflow-backend's document-content view API
    (the read-before-write optimistic lock). A 409 response means the document
    changed since the editor loaded it — the FE should reload and retry.
    """
    import os as _os

    from plugins.db import _validate_id, get_workspace_context
    from plugins.document_repo import StaleBaseError, write_document
    from plugins.tools.artifacts import _resolve_management_repo

    _DOCUMENT_FILES = {
        "product_spec": "product-spec.md",
        "technical_design": "technical-design.md",
    }

    filename = _DOCUMENT_FILES.get(body.doc)
    if filename is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown document type: {body.doc!r}. Must be one of {list(_DOCUMENT_FILES)}.",
        )

    try:
        _validate_id(feature_id, "feature_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    github_token = _os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN is not configured on the server.")

    # Workspace resolution: infer workspace from the feature context if needed.
    # For the human-save path we look up the workspace via the feature context
    # stored in the agent session. As a fallback we read WORKSPACE_ID from env.
    workspace_id = _os.environ.get("WORKSPACE_ID", "").strip()
    if not workspace_id:
        raise HTTPException(
            status_code=500,
            detail="WORKSPACE_ID is not configured — cannot resolve management repo.",
        )

    try:
        workspace_context = get_workspace_context(workspace_id)
        owner, repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not resolve management repo: {exc}",
        ) from exc

    base_branch = _os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")
    path = f"docs/features/{feature_id}/{filename}"
    commit_message = f"docs: human save {body.doc.replace('_', '-')} (via hermes)"

    try:
        result = write_document(
            owner, repo, feature_id, base_branch, path,
            body.content, body.base_sha, commit_message, github_token,
        )
    except StaleBaseError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "conflict": True,
                "message": "Document changed since you opened it. Reload and retry.",
                "detail": str(exc),
            },
        ) from exc
    except Exception as exc:
        logger.exception("save_document failed for feature %s", feature_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({
        "ok": True,
        "commit_sha": result["commit_sha"],
        "pr": result["pr"],
    })


# ---------------------------------------------------------------------------
# GET /tools — live tool registry (honoring check_fn gating)
# ---------------------------------------------------------------------------


@router.get("/tools")
async def list_tools_endpoint() -> JSONResponse:
    """Return the live tool list from the plugin registry, honoring each tool's check_fn.

    Gated-off tools (where check_fn returns False) are excluded.
    Used by the FE slash-command picker to list available tools accurately.
    """
    from plugins import _TOOLS

    tools = []
    for t in _TOOLS:
        check_fn = t.get("check_fn")
        if check_fn is not None:
            try:
                if not check_fn():
                    continue
            except Exception:
                continue
        tools.append({
            "name": t["name"],
            "description": t.get("schema", {}).get("description", ""),
        })
    return JSONResponse({"tools": tools})


# ---------------------------------------------------------------------------
# POST /features/{feature_id}/stage-transition — ts lifecycle write
# ---------------------------------------------------------------------------

_STAGE_ORDER = ["product_spec", "technical_design", "tasks", "handoff"]

_APPROVE_EFFECTS: dict[str, dict] = {
    "product_spec": {
        "feature_status": "in_tdd",
        "current_stage": "technical_design",
        "next_action": "Technical design required. Use the tech-lead skill (Phase 1).",
    },
    "technical_design": {
        "feature_status": "in_tdd",
        "current_stage": "tasks",
        "next_action": "Task breakdown required. Use the tech-lead skill (Phase 2).",
    },
    "tasks": {
        "feature_status": "ready_for_implementation",
        "current_stage": "handoff",
        "next_action": "Tasks ready for implementation.",
    },
    "handoff": {
        "feature_status": "done",
        "current_stage": "done",
        "next_action": "Feature complete.",
    },
}

_REOPEN_EFFECTS: dict[str, dict] = {
    "product_spec": {
        "feature_status": "in_design",
        "current_stage": "product_spec",
        "revalidation": {"technical_design_required": True, "tasks_required": True},
        "next_action": "Product spec reopened. Update the artifact and re-submit for approval.",
    },
    "technical_design": {
        "feature_status": "in_tdd",
        "current_stage": "technical_design",
        "revalidation": {"tasks_required": True},
        "next_action": "Technical design reopened. Update the artifact and re-submit for approval.",
    },
    "tasks": {
        "feature_status": "in_tdd",
        "current_stage": "tasks",
        "revalidation": {},
        "next_action": "Tasks reopened. Update the task breakdown and re-submit for approval.",
    },
    "handoff": {
        "feature_status": "ready_for_implementation",
        "current_stage": "handoff",
        "revalidation": {},
        "next_action": "Handoff reopened. Update the handoff artifact and re-submit.",
    },
}


class StageTransitionRequest(BaseModel):
    stage: str
    action: str  # "approve" | "reject" | "reopen"
    comment: Optional[str] = None


@router.post("/features/{feature_id}/stage-transition")
async def stage_transition_endpoint(
    feature_id: str,
    body: StageTransitionRequest,
    identity: Identity = Depends(require_identity),
) -> JSONResponse:
    """Commit a stage-review state change to status.yaml on the feature branch.

    Only operates on ``ts`` features (those with a ``status.yaml`` in the
    management repo). ``go`` features are out of scope for v3.

    actor = ``X-User-Id`` from ``require_identity``.
    """
    import datetime
    import os as _os

    import yaml

    from plugins.db import _validate_id, get_workspace_context
    from plugins.document_repo import StaleBaseError, read_document, write_document
    from plugins.tools.artifacts import _resolve_management_repo

    _VALID_STAGES = {"product_spec", "technical_design", "tasks", "handoff"}
    _VALID_ACTIONS = {"approve", "reject", "reopen"}

    if body.stage not in _VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid stage {body.stage!r}. Must be one of {sorted(_VALID_STAGES)}.",
        )
    if body.action not in _VALID_ACTIONS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid action {body.action!r}. Must be one of {sorted(_VALID_ACTIONS)}.",
        )

    try:
        _validate_id(feature_id, "feature_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    github_token = _os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN is not configured on the server.")

    workspace_id = _os.environ.get("WORKSPACE_ID", "").strip()
    if not workspace_id:
        raise HTTPException(
            status_code=500,
            detail="WORKSPACE_ID is not configured — cannot resolve management repo.",
        )

    try:
        workspace_context = get_workspace_context(workspace_id)
        owner, repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not resolve management repo: {exc}",
        ) from exc

    base_branch = _os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")
    branch = f"feature/{feature_id}"
    path = f"docs/features/{feature_id}/status.yaml"
    actor = identity.user_id

    # Read status.yaml from the feature branch.
    try:
        current = read_document(owner, repo, branch, path, github_token)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not read status.yaml: {exc}",
        ) from exc

    if not current["content"]:
        raise HTTPException(
            status_code=404,
            detail=(
                f"status.yaml not found for feature {feature_id!r} on branch {branch!r}. "
                "This endpoint operates on ts features only."
            ),
        )

    try:
        status_data: dict = yaml.safe_load(current["content"])
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not parse status.yaml: {exc}",
        ) from exc

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+0000")
    stage_block = status_data.setdefault("stages", {}).setdefault(body.stage, {})
    if "review_history" not in stage_block or stage_block["review_history"] is None:
        stage_block["review_history"] = []
    if "history" not in status_data or status_data["history"] is None:
        status_data["history"] = []

    if body.action == "approve":
        stage_block["review_status"] = "approved"
        stage_block["reviewed_by"] = actor
        stage_block["reviewed_at"] = now
        stage_block["review_comment"] = body.comment
        stage_block["review_history"].append({
            "review_status": "approved",
            "reviewed_by": actor,
            "reviewed_at": now,
            "comment": body.comment,
        })
        effects = _APPROVE_EFFECTS.get(body.stage, {})
        if effects:
            status_data["feature_status"] = effects["feature_status"]
            status_data["current_stage"] = effects["current_stage"]
            status_data["next_action"] = effects["next_action"]
        status_data["history"].append({
            "at": now,
            "by": actor,
            "action": "stage_approved",
            "stage": body.stage,
            "note": f"{body.stage} approved by {actor}.",
        })
        commit_msg = f"chore({feature_id}): approve {body.stage} stage"

    elif body.action == "reject":
        stage_block["review_status"] = "rejected"
        stage_block["reviewed_by"] = actor
        stage_block["reviewed_at"] = now
        stage_block["review_comment"] = body.comment
        stage_block["review_history"].append({
            "review_status": "rejected",
            "reviewed_by": actor,
            "reviewed_at": now,
            "comment": body.comment,
        })
        status_data["next_action"] = (
            f"Stage {body.stage} rejected. Address the comment and re-submit for approval."
        )
        status_data["history"].append({
            "at": now,
            "by": actor,
            "action": "stage_rejected",
            "stage": body.stage,
            "note": f"{body.stage} rejected by {actor}. Comment: {body.comment or '(none)'}",
        })
        commit_msg = f"chore({feature_id}): reject {body.stage} stage"

    else:  # reopen
        stage_block["review_status"] = "draft"
        stage_block["reviewed_by"] = None
        stage_block["reviewed_at"] = None
        stage_block["review_comment"] = None
        stage_block["review_history"].append({
            "review_status": "draft",
            "reviewed_by": actor,
            "reviewed_at": now,
            "comment": f"Stage reopened by {actor}.",
        })
        effects = _REOPEN_EFFECTS.get(body.stage, {})
        if effects:
            status_data["feature_status"] = effects["feature_status"]
            status_data["current_stage"] = effects["current_stage"]
            status_data["next_action"] = effects["next_action"]
            revalidation = status_data.setdefault("revalidation", {})
            for k, v in effects.get("revalidation", {}).items():
                revalidation[k] = v
        status_data["history"].append({
            "at": now,
            "by": actor,
            "action": "stage_reopened",
            "stage": body.stage,
            "note": f"{body.stage} reopened by {actor} — artifacts preserved, revalidation flags set.",
        })
        commit_msg = f"chore({feature_id}): reopen {body.stage} stage"

    new_content = yaml.dump(status_data, default_flow_style=False, allow_unicode=True)

    try:
        result = write_document(
            owner, repo, feature_id, base_branch, path,
            new_content, current["sha"], commit_msg, github_token,
        )
    except StaleBaseError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "conflict": True,
                "message": "status.yaml changed since you read it. Retry.",
                "detail": str(exc),
            },
        ) from exc
    except Exception as exc:
        logger.exception("stage_transition failed for feature %s", feature_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({
        "ok": True,
        "feature_id": feature_id,
        "stage": body.stage,
        "action": body.action,
        "review_status": stage_block["review_status"],
        "commit_sha": result["commit_sha"],
        "pr": result["pr"],
    })
