"""FastAPI application factory for the workflow gateway.

Entry point:
    uvicorn src.app:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import logging
import os
import pathlib
from contextlib import asynccontextmanager
from typing import AsyncIterator

from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent / ".env")

# Register the workflow plugin before anything else imports model_tools.
# Our workflow plugin (the top-level plugins/ package) is not on the agent's
# plugin search path, so it must be wired up explicitly here.
try:
    import plugins as _plugins
    from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

    _wf_manifest = PluginManifest(name="workflow", source="bundled", kind="backend")
    _wf_ctx = PluginContext(_wf_manifest, get_plugin_manager())
    _plugins.register(_wf_ctx)
except Exception as _exc:
    import logging as _logging
    _logging.getLogger(__name__).warning(
        "src: failed to register workflow plugin: %s", _exc
    )

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api.router import router
from src.db import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Configure it in the environment (e.g. postgresql://user:pass@host/db)."
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

    app.include_router(router, prefix="/api/v1")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
