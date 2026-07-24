"""Admin model catalog routes.

GET    /admin/models               — list all catalog entries (active + inactive)
POST   /admin/models               — create a new catalog entry
PATCH  /admin/models/{model_id}    — update display_name / is_active / is_default

All routes require the caller to hold the ``platform_admin`` role, enforced by
the ``require_platform_admin`` dependency (fail-closed role check against
user-service).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import get_db
from src.api.identity import Identity, require_platform_admin
from src.db import store as db_store

router = APIRouter(prefix="/admin")

# Providers with credential wiring in hermes-agent. A new provider requires a
# code change to resolve_model() — the admin UI does not make provider wiring
# data-driven.
_ALLOWED_PROVIDERS = {"anthropic", "deepseek"}


class CreateModelRequest(BaseModel):
    model_id: str
    display_name: str
    provider: str
    is_active: bool = True
    is_default: bool = False

    @field_validator("provider")
    @classmethod
    def validate_provider(cls, v: str) -> str:
        if v not in _ALLOWED_PROVIDERS:
            raise ValueError(f"provider must be one of {sorted(_ALLOWED_PROVIDERS)}")
        return v

    @field_validator("model_id")
    @classmethod
    def validate_model_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("model_id must not be empty")
        return v

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("display_name must not be empty")
        return v


class PatchModelRequest(BaseModel):
    display_name: str | None = None
    is_active: bool | None = None
    is_default: bool | None = None

    @field_validator("display_name")
    @classmethod
    def validate_display_name(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip()
            if not v:
                raise ValueError("display_name must not be empty")
        return v


def _model_row(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return row
    return {
        "model_id": row.model_id,
        "display_name": row.display_name,
        "provider": row.provider,
        "is_active": row.is_active,
        "is_default": row.is_default,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


@router.get("/models")
async def list_admin_models(
    db: Annotated[AsyncSession, Depends(get_db)],
    _identity: Annotated[Identity, Depends(require_platform_admin)],
) -> JSONResponse:
    """Return all catalog entries (active and inactive)."""
    rows = await db_store.list_catalog_models(db)
    return JSONResponse({"models": rows})


@router.post("/models", status_code=201)
async def create_admin_model(
    body: CreateModelRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _identity: Annotated[Identity, Depends(require_platform_admin)],
) -> JSONResponse:
    """Create a new model catalog entry.

    Validates that the provider is one of the known credential-wired providers.
    Returns 409 if model_id already exists.
    """
    try:
        row = await db_store.create_catalog_model(
            db,
            model_id=body.model_id,
            display_name=body.display_name,
            provider=body.provider,
            is_active=body.is_active,
            is_default=body.is_default,
        )
    except IntegrityError:
        raise HTTPException(status_code=409, detail=f"Model '{body.model_id}' already exists.")

    return JSONResponse(_model_row(row), status_code=201)


@router.patch("/models/{model_id}")
async def patch_admin_model(
    model_id: str,
    body: PatchModelRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    _identity: Annotated[Identity, Depends(require_platform_admin)],
) -> JSONResponse:
    """Update a catalog entry.

    - Setting ``is_default: true`` clears the previous default in the same transaction.
    - Setting ``is_active: false`` on the current default model is rejected with 400
      — the admin must reassign the default first.
    - Returns 404 if model_id is not found.
    """
    if body.display_name is None and body.is_active is None and body.is_default is None:
        raise HTTPException(status_code=400, detail="No fields to update.")

    try:
        row = await db_store.update_catalog_model(
            db,
            model_id,
            display_name=body.display_name,
            is_active=body.is_active,
            is_default=body.is_default,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if row is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found.")

    return JSONResponse(_model_row(row))
