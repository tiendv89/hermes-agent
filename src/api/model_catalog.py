"""Catalog of user-selectable chat models and how to run each one.

Model identity (id, display name, provider, active/default flags) is now stored
in the ``model_catalog`` table and managed via the admin API. This module queries
the table directly — no network hop, no cache, no staleness window.

The workflow gateway exposes this list via ``GET /api/v1/models`` so the
front-end can render a model picker, and resolves a chosen model id to the
provider + credentials used to construct the agent for a turn.

There is no server-side default model — callers must supply an explicit,
active catalog model id, or ``resolve_model`` raises ``ValueError``.
"""

from __future__ import annotations

import logging
import os
from typing import TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from src.db import store as db_store

logger = logging.getLogger(__name__)


class ModelInfo(TypedDict):
    id: str
    label: str
    provider: str


async def get_active_models(db: AsyncSession) -> list[ModelInfo]:
    """Return all active catalog models as ModelInfo dicts."""
    rows = await db_store.list_active_catalog_models(db)
    return [{"id": r["model_id"], "label": r["display_name"], "provider": r["provider"]} for r in rows]


async def is_supported(db: AsyncSession, model_id: str) -> bool:
    """True if model_id exists in the catalog and is active."""
    row = await db_store.get_catalog_model(db, model_id)
    return row is not None and row.is_active


class ResolvedModel(TypedDict):
    model: str
    provider: str
    api_key: str | None
    base_url: str | None


async def resolve_model(db: AsyncSession, model_id: str) -> ResolvedModel:
    """Map a catalog model id to the provider + credentials used to run it.

    Raises ``ValueError`` if ``model_id`` is empty, or not an active catalog
    entry — there is no server-side default to fall back to.
    """
    target_id = (model_id or "").strip()
    if not target_id:
        raise ValueError("model is required")

    row = await db_store.get_catalog_model(db, target_id)
    if row is None or not row.is_active:
        raise ValueError(f"Unknown or inactive model: {target_id!r}")

    provider = row.provider
    if provider == "deepseek":
        return {
            "model": row.model_id,
            "provider": "deepseek",
            "api_key": os.environ.get("DEEPSEEK_API_KEY") or None,
            "base_url": os.environ.get("DEEPSEEK_BASE_URL") or None,
        }

    # Default / anthropic: Anthropic Messages API, key from ANTHROPIC_API_KEY.
    return {
        "model": row.model_id,
        "provider": provider,
        "api_key": os.environ.get("ANTHROPIC_API_KEY") or None,
        "base_url": None,
    }
