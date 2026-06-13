"""Model catalog route.

    GET /models — selectable chat models for the FE picker
"""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.api.model_catalog import SUPPORTED_MODELS, default_model

router = APIRouter()


@router.get("/models")
async def list_models_endpoint() -> JSONResponse:
    """Return the supported chat models (Claude + DeepSeek) and the server default.

    Static catalog — no identity required; the picker fetches it once on load.
    """
    return JSONResponse({"models": SUPPORTED_MODELS, "default": default_model()})
