"""FastAPI application factory for the workflow gateway.

Entry point:
    uvicorn workflow_gateway.app:app --host 0.0.0.0 --port 8000

The gateway embeds AIAgent as an in-process library (swell-hermes pattern)
and exposes a voyager-compatible SSE envelope to workflow-backend.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import asyncpg
from fastapi import FastAPI

from workflow_gateway.api.router import router
from workflow_gateway.sessions import init_db

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the asyncpg pool on startup, close it on shutdown."""
    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        raise RuntimeError(
            "DATABASE_URL is not set. "
            "Configure it in the environment (e.g. postgresql://user:pass@host/db)."
        )

    pool = await asyncpg.create_pool(database_url, min_size=2, max_size=10)
    await init_db(pool)
    app.state.db_pool = pool
    logger.info("workflow_gateway: Postgres pool ready")

    yield

    await pool.close()
    logger.info("workflow_gateway: Postgres pool closed")


def create_app() -> FastAPI:
    """Construct and return the FastAPI application."""
    app = FastAPI(
        title="hermes workflow gateway",
        description="Workspace-aware AI agent gateway (digital-factory / M3)",
        version="1.0.0",
        lifespan=_lifespan,
    )

    app.include_router(router, prefix="/api/v5")

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    return app


app = create_app()
