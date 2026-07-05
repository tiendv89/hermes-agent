"""Async, cached accessor for workflow-backend's Postgres database.

hermes-agent already has direct read access to workflow-backend's DB via
WORKFLOW_DATABASE_URL (see plugins/db.py, used by the LLM tool-call plugins
for workspace/feature context). Reused here to resolve a workspace's
organization_id for org-scoped permission/membership checks (channel-delete
admin gate, DM eligibility, @mention candidates) — no new HTTP endpoint or
schema column needed; workspace->org ownership is looked up live and cached
briefly, so it never goes stale relative to workflow-backend's own data.

``plugins.db`` is imported lazily (inside the function, not at module level)
so that importing this module — and by extension the API routers that use
it — never pulls in the plugins package unless a lookup is actually
attempted with WORKFLOW_DATABASE_URL configured.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

_TTL_SECONDS = 30.0
_cache: Dict[str, Tuple[float, str]] = {}


async def get_workspace_organization_id(workspace_id: str) -> str:
    """Return the organization_id owning workspace_id, or "" if unknown/unavailable.

    Returns "" (permissive — callers should treat this as "skip org-scoped
    checks") when WORKFLOW_DATABASE_URL is unset, the workspace isn't found,
    or the lookup errors.
    """
    if not workspace_id or not os.environ.get("WORKFLOW_DATABASE_URL", "").strip():
        return ""

    cached = _cache.get(workspace_id)
    if cached and (time.monotonic() - cached[0]) < _TTL_SECONDS:
        return cached[1]

    try:
        from plugins.db import get_workspace_organization_id as _sync_lookup

        org_id = await asyncio.to_thread(_sync_lookup, workspace_id)
    except Exception:
        logger.exception("workflow-backend org_id lookup failed for workspace %s", workspace_id)
        return ""

    org_id = org_id or ""
    _cache[workspace_id] = (time.monotonic(), org_id)
    return org_id
