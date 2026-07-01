"""Catalog of user-selectable chat models and how to run each one.

Model identity (id, display name, provider, active/default flags) is now stored
in the ``model_catalog`` table and managed via the admin API. This module queries
the table directly — no network hop, no cache, no staleness window.

The workflow gateway exposes this list via ``GET /api/v1/models`` so the
front-end can render a model picker, and resolves a chosen model id to the
provider + credentials used to construct the agent for a turn.

**Defense-in-depth fallback**: if the catalog table is empty or the DB is
unavailable at lookup time, ``_FALLBACK_MODEL_ID`` is used. This covers the
window between first deploy and the first migration run.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional, TypedDict

from sqlalchemy.ext.asyncio import AsyncSession

from src.db import store as db_store

logger = logging.getLogger(__name__)

# Built-in fallback — used only when the catalog table has no active rows.
# This is intentionally minimal (one model) so a misconfigured catalog fails
# loudly rather than silently presenting a stale hardcoded list.
_FALLBACK_MODEL_ID = "claude-sonnet-4-6"


class ModelInfo(TypedDict):
    id: str
    label: str
    provider: str


async def get_active_models(db: AsyncSession) -> List[ModelInfo]:
    """Return all active catalog models as ModelInfo dicts."""
    rows = await db_store.list_active_catalog_models(db)
    return [{"id": r["model_id"], "label": r["display_name"], "provider": r["provider"]} for r in rows]


async def is_supported(db: AsyncSession, model_id: str) -> bool:
    """True if model_id exists in the catalog and is active."""
    row = await db_store.get_catalog_model(db, model_id)
    return row is not None and row.is_active


async def default_model(db: AsyncSession) -> str:
    """The server default model id.

    Resolution order:
    1. ``HERMES_MODEL`` env var (ops-level emergency override) — if it names an
       active catalog model.
    2. The catalog row with ``is_default=True``.
    3. ``_FALLBACK_MODEL_ID`` (defense in depth, e.g. empty catalog).
    """
    env = os.environ.get("HERMES_MODEL", "").strip()
    if env:
        row = await db_store.get_catalog_model(db, env)
        if row is not None and row.is_active:
            return env

    row = await db_store.get_default_catalog_model(db)
    if row is not None:
        return row.model_id

    return _FALLBACK_MODEL_ID


class ResolvedModel(TypedDict):
    model: str
    provider: str
    api_key: Optional[str]
    base_url: Optional[str]


async def resolve_model(db: AsyncSession, model_id: str) -> ResolvedModel:
    """Map a catalog model id to the provider + credentials used to run it.

    An unknown or inactive id falls back to the server default so a turn can
    never be wedged by a bad model string.
    """
    target_id = (model_id or "").strip()
    row = await db_store.get_catalog_model(db, target_id)
    if row is None or not row.is_active:
        fallback_id = await default_model(db)
        row = await db_store.get_catalog_model(db, fallback_id)

    if row is None:
        # Last-resort: use the hardcoded fallback id with anthropic defaults.
        return {
            "model": _FALLBACK_MODEL_ID,
            "provider": "anthropic",
            "api_key": os.environ.get("ANTHROPIC_API_KEY") or None,
            "base_url": None,
        }

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
