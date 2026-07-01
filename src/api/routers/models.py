"""Model catalog route.

GET /models — selectable chat models for the FE picker
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.model_catalog import default_model, get_active_models

router = APIRouter()


@router.get("/models")
async def list_models_endpoint(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """Return the supported chat models and the server default.

    Response shape is unchanged — {models: [{id, label, provider}], default}.
    The catalog is now read from the model_catalog table instead of a hardcoded list.
    """
    models = await get_active_models(db)
    default = await default_model(db)
    return JSONResponse({"models": models, "default": default})
