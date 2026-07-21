"""FastAPI application factory for the workflow gateway.

Entry point:
    uvicorn src.app:app --host 0.0.0.0 --port 8000

Profile selection:
    Set ``HERMES_PROFILE`` to ``workflow`` (default) or ``coding`` before
    startup.  Each profile registers a distinct tool set and mounts its own
    FastAPI router.

    Workflow: BFF-proxied web chat with document-editing, workflow-mutation,
              and VCS tools.
    Coding:   IDE pair-programming with client-executed tools (T3/T4).
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

# ---------------------------------------------------------------------------
# Profile selection
# ---------------------------------------------------------------------------

_HERMES_PROFILE = os.getenv("HERMES_PROFILE", "workflow").strip().lower()
logger = logging.getLogger(__name__)
logger.info("src: selected profile: %s", _HERMES_PROFILE)

# ---------------------------------------------------------------------------
# Register profile tools before anything else imports model_tools.
# Our workflow plugin (the top-level plugins/ package) is not on the agent's
# plugin search path, so it must be wired up explicitly here via the profile
# setup module.
# ---------------------------------------------------------------------------

try:
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    # Resolve the profile setup module.
    if _HERMES_PROFILE == "workflow":
        from profiles.workflow.setup import register_tools, build_router
    elif _HERMES_PROFILE == "coding":
        from profiles.coding.setup import register_tools, build_router
    else:
        raise ValueError(
            f"Unknown HERMES_PROFILE value: {_HERMES_PROFILE!r}. "
            f"Expected 'workflow' or 'coding'."
        )

    _manifest = PluginManifest(
        name=_HERMES_PROFILE,
        source="bundled",
        kind="backend",
    )
    _ctx = PluginContext(_manifest, get_plugin_manager())
    register_tools(_ctx)
    logger.info("src: profile %s tools registered", _HERMES_PROFILE)
except Exception as _exc:
    import logging as _logging

    _logging.getLogger(__name__).warning(
        "src: failed to register %s profile tools: %s", _HERMES_PROFILE, _exc
    )

    # Fallback: import the workflow profile directly (this keeps the app
    # functional even if the dynamic import above fails).
    from profiles.workflow.setup import register_tools as register_tools
    from profiles.workflow.setup import build_router as build_router


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


def create_app() -> FastAPI:
    app = FastAPI(
        title="hermes workflow gateway",
        description="Workspace-aware AI agent gateway (digital-factory / M3)",
        version="1.0.0",
        lifespan=_lifespan,
    )

    app.include_router(build_router(), prefix="/api/v1")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
