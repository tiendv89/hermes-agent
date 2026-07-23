"""FastAPI application factory for the workflow gateway.

Entry point:
    uvicorn src.app:app --host 0.0.0.0 --port 8000

One process, one chat surface: POST /api/v1/chat serves both the browser
(document-editing, workflow-mutation, VCS tools) and the IDE extension
(client-executed file/git/terminal tools via opencode) — there is no more
per-deployment ``HERMES_PROFILE`` split and no separate /coding/* router.
Per-turn intent triage (src/api/triage.py) decides whether a turn stays on
the Hermes workflow agent or delegates to opencode; an ``ide_context`` field
on the request body (see src/api/routers/chat.py) tells the coding-verdict
branch it can actually dispatch to opencode instead of redirecting to the
IDE (see src/api/agent_dispatch.py).

src/tool_setup.py still registers both the workflow and coding tool sets at
import time below (under their own toolset name, plus a "shared" toolset
for tools both use — see plugins/__init__.py::register()) — the coding
tool set's client-executed (deferred) tools are still needed by opencode's
MCP bridge (src/mcp/coding_bridge_server.py), it just no longer mounts its
own router.
"""

from __future__ import annotations

import logging
import os
import pathlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

load_dotenv(pathlib.Path(__file__).parent / ".env")

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Register every tool before anything else imports model_tools. Our workflow
# plugin (the top-level plugins/ package) is not on the agent's plugin search
# path, so it must be wired up explicitly here via src/tool_setup.py, which
# passes each tool's own toolset name through to plugins.register() so the
# workflow and coding tool sets coexist in the one shared tool registry
# without shadowing each other.
# ---------------------------------------------------------------------------

from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

from src.tool_setup import build_router, register_tools

try:
    _manifest = PluginManifest(name="hermes-agent", source="bundled", kind="backend")
    _ctx = PluginContext(_manifest, get_plugin_manager())
    register_tools(_ctx)
    logger.info("src: tools registered")
except Exception:
    logger.exception("src: failed to register tools — every tool call will 500")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Imported lazily: src.db pulls in model_tools, which must not load until
    # after the workflow plugin is registered (at module import, above).
    from src.db import init_db

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Configure it in the environment (e.g. postgresql://user:***@host/db)."
        )

    # SQLAlchemy asyncpg driver requires the postgresql+asyncpg:// scheme.
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+asyncpg://", 1)

    engine = create_async_engine(database_url, pool_size=5, max_overflow=10)
    await init_db(engine)

    app.state.db_session = async_sessionmaker(engine, expire_on_commit=False)
    logger.info("src: database ready")

    yield

    await engine.dispose()
    logger.info("src: database connection closed")


class _SessionScopedMCPBridge:
    """ASGI app lazily building/caching one MCP bridge app per coding session.

    Mounted at ``/api/v1/coding/mcp-bridge`` — Starlette's ``Mount`` strips
    that prefix before invoking us, so ``scope["path"]`` arrives as
    ``/<session_id>/...``. The session_id segment selects (or builds) that
    session's ``coding_bridge_server`` app; one shared static mount can't
    disambiguate which in-flight coding session a request belongs to, since
    each session needs its own translator/session_id closure (see
    ``src/mcp/coding_bridge_server.py``).

    FastMCP's streamable-http transport needs its ``session_manager.run()``
    async-context-manager entered before it can serve requests — normally
    done by the ASGI server invoking the sub-app's own ``lifespan``, which
    never happens for an app built and mounted lazily after this process's
    own lifespan already started. ``anyio`` task groups are task-affine
    (entering and exiting a cancel scope in different tasks raises), so
    that context manager can't just be entered inside one request's task
    and exited later from an unrelated one — instead, each session gets a
    dedicated long-lived "owner" task that holds the context manager open
    for the session's lifetime and tears it down when ``discard()`` signals
    it to stop.
    """

    def __init__(self) -> None:
        import asyncio as _asyncio

        self._apps: dict = {}
        self._owners: dict = {}  # session_id -> (owner_task, stop_event)
        self._lock = _asyncio.Lock()

    async def _get_or_create(self, session_id: str):
        import asyncio as _asyncio

        async with self._lock:
            sub_app = self._apps.get(session_id)
            if sub_app is not None:
                return sub_app

            from src.mcp.coding_bridge_server import build_bridge_app

            mcp_instance = build_bridge_app(session_id)
            sub_app = mcp_instance.streamable_http_app()

            ready = _asyncio.Event()
            stop = _asyncio.Event()

            async def _owner() -> None:
                async with mcp_instance.session_manager.run():
                    ready.set()
                    await stop.wait()

            owner_task = _asyncio.ensure_future(_owner())
            await ready.wait()

            self._apps[session_id] = sub_app
            self._owners[session_id] = (owner_task, stop)
            return sub_app

    async def discard(self, session_id: str) -> None:
        """Tear down a session's bridge app once its coding turn ends."""
        async with self._lock:
            self._apps.pop(session_id, None)
            owner = self._owners.pop(session_id, None)
        if owner is not None:
            owner_task, stop = owner
            stop.set()
            try:
                await owner_task
            except Exception:
                logger.exception(
                    "coding_mcp_bridge: error tearing down session %s", session_id
                )

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        # This Starlette version hands sub-apps the FULL original path plus
        # a root_path marking what the parent Mount already consumed — it
        # does NOT pre-strip the mount prefix from scope["path"] the way
        # older Starlette releases did. Strip it ourselves.
        root_path = scope.get("root_path", "")
        full_path = scope.get("path", "")
        relative = full_path[len(root_path):] if root_path and full_path.startswith(root_path) else full_path
        session_id, _, rest = relative.lstrip("/").partition("/")
        if not session_id:
            from starlette.responses import PlainTextResponse

            response = PlainTextResponse("missing session id", status_code=404)
            await response(scope, receive, send)
            return
        sub_app = await self._get_or_create(session_id)
        new_scope = dict(scope)
        new_scope["path"] = "/" + rest
        new_scope["root_path"] = f"{root_path}/{session_id}"
        await sub_app(new_scope, receive, send)


def create_app() -> FastAPI:
    app = FastAPI(
        title="workflow gateway",
        description="Workspace-aware AI agent gateway",
        version="1.0.0",
        lifespan=_lifespan,
    )

    app.include_router(build_router(), prefix="/api/v1")

    coding_mcp_bridge = _SessionScopedMCPBridge()
    app.state.coding_mcp_bridge = coding_mcp_bridge
    app.mount("/api/v1/coding/mcp-bridge", coding_mcp_bridge)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
