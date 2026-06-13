"""FastAPI router for the workflow gateway.

Routes:
    POST /session — create a new session
    GET /sessions — list sessions for a workspace+feature
    GET /sessions/{session_id}/messages — load a session's transcript
    POST /chat — run one agent turn and stream SSE back
    PUT /features/{feature_id}/document — human-save a document to the feature branch
    GET /tools — live tool registry + loadable skills (for the FE slash-command picker)
    POST /features/{feature_id}/stage-transition — ts lifecycle write (approve/reject/reopen)

The router is mounted at ``/api/v1`` in ``src/app.py``.
"""

from __future__ import annotations

import asyncio
import functools
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
    update_session_model,
)
from src.api.identity import Identity, require_identity
from src.api.model_catalog import (
    SUPPORTED_MODELS,
    default_model,
    resolve_model,
)
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
    # Catalog model id (see model_catalog). Empty → reuse the session's model,
    # then the server default. Unknown ids fall back to the default.
    model: str = ""


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
# GET /models — selectable chat models for the FE picker
# ---------------------------------------------------------------------------


@router.get("/models")
async def list_models_endpoint() -> JSONResponse:
    """Return the supported chat models (Claude + DeepSeek) and the server default.

    Static catalog — no identity required; the picker fetches it once on load.
    """
    return JSONResponse({"models": SUPPORTED_MODELS, "default": default_model()})


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
# POST /chat (alias: /stream_chat) — run one agent turn and stream SSE back
# ---------------------------------------------------------------------------


def _derive_title(message: str) -> str:
    """First 60 chars of the opening message — used to auto-title a session."""
    first_line = message.strip().splitlines()[0] if message.strip() else ""
    return first_line[:60] or "New chat"


def _run_agent_turn(
    *,
    session_id: str,
    message: str,
    history: list,
    workspace_id: str,
    feature_id: str,
    user_id: str,
    model: str,
    provider: Optional[str],
    api_key: Optional[str],
    base_url: Optional[str],
    db_factory,
    loop: asyncio.AbstractEventLoop,
    translator: HermesSSETranslator,
) -> None:
    """Run one blocking agent turn on a worker thread, streaming via *translator*.

    This owns the run lifecycle end-to-end: whatever happens, it finalizes the
    SSE stream (``translator.done`` / ``on_error``) and releases the in-flight
    marker so the session can accept the next turn. The HTTP handler returns as
    soon as this is scheduled — the response body is driven by the translator's
    async queue, which this function feeds from the worker thread.
    """
    workflow_context = None
    try:
        # Tool handlers and the pre_llm_call hook read the active workspace /
        # feature from this thread-local context. The executor pool is reused
        # across sessions, so it has to be (re)set at the start of every turn.
        from plugins import context as workflow_context
        workflow_context.set_context(session_id, workspace_id, feature_id)

        # Mirror the agent's transcript writes into Postgres. Best-effort: if
        # the proxy can't be built we still run the turn, just unmirrored.
        try:
            session_db = make_gateway_session_db(loop, db_factory, session_id)
        except Exception:
            logger.exception(
                "chat: gateway session DB unavailable for %s; transcript not mirrored",
                session_id,
            )
            session_db = None

        # The bundled shared workflow rules (plugins/skills/shared.md) are appended to
        # the agent's system prompt every turn so it always follows the company
        # workflow (feature lifecycle, stage-review + task statuses, the flow).
        from plugins.skills import get_shared_rules
        shared_rules = get_shared_rules() or None

        # Heavyweight agent deps are imported here so they only load when a turn
        # actually runs (and so tests can stub `run_agent` in sys.modules).
        from run_agent import AIAgent

        agent = AIAgent(
            model=model,
            provider=provider,
            api_key=api_key,
            base_url=base_url,
            enabled_toolsets=["workflow"],
            max_iterations=int(os.environ.get("HERMES_MAX_ITERATIONS", "90")),
            quiet_mode=True,
            platform="workflow_gateway",
            ephemeral_system_prompt=shared_rules,
            session_id=session_id,
            user_id=user_id or None,
            gateway_session_key=session_id,
            session_db=session_db,
            stream_delta_callback=translator.on_delta,
            tool_start_callback=translator.on_tool_start,
            tool_complete_callback=translator.on_tool_complete,
        )
        agent.run_conversation(message, conversation_history=history)
    except Exception as exc:  # noqa: BLE001 — any failure must reach the client
        logger.exception("chat: agent turn failed for session %s", session_id)
        translator.on_error(str(exc))
    finally:
        translator.done()
        if workflow_context is not None:
            workflow_context.clear_context(session_id)
        with _active_runs_lock:
            _active_runs.discard(session_id)


@router.post("/chat")
async def stream_chat(
    body: StreamChatRequest,
    request: Request,
    identity: Identity = Depends(require_identity),
    db: AsyncSession = Depends(_get_db),
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
    """Return the live tools + loadable skills for the FE slash-command picker.

    ``tools`` is the plugin registry, honoring each tool's check_fn (gated-off
    tools, where check_fn returns False, are excluded).

    ``skills`` is the bundled skill index — every entry is loadable on demand
    via the ``load_skill`` tool. Each carries a ``type`` of
    ``"technical"`` (knowledge skills) or ``"workflow"`` (workflow skills).
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

    skills = []
    try:
        from plugins.skills import get_index

        for entry in sorted(get_index().values(), key=lambda e: e.name):
            skills.append({
                "name": entry.name,
                "description": entry.description,
                "type": "workflow" if entry.is_authoring else "technical",
            })
    except Exception:
        logger.exception("list_tools: failed to build the skill list")

    return JSONResponse({"tools": tools, "skills": skills})


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
