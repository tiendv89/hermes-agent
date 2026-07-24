"""Shared FastAPI dependencies for the workflow gateway routers."""

from __future__ import annotations

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """Yield a DB session from the app-state sessionmaker (per-request)."""
    async with request.app.state.db_session() as session:
        yield session
