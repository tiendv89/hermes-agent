"""Model catalog route.

GET /models — selectable chat models for the FE picker
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.model_catalog import get_active_models

router = APIRouter()


@router.get("/models")
async def list_models_endpoint(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """Return the supported chat models.

    There is no server-side default model — callers must always pass an
    explicit ``model`` id when starting or continuing a conversation.
    """
    models = await get_active_models(db)
    return JSONResponse({"models": models})
