"""Cached accessor for workflow-backend's workspace->organization lookup.

Wraps ``src.services.workflow_backend_client.get_workspace_organization_id``
(an HTTP call to workflow-backend) with a short-TTL in-process cache. Used to
resolve a workspace's organization_id for org-scoped permission/membership
checks (channel-delete admin gate, DM eligibility, @mention candidates) —
looked up live and cached briefly, so it never goes stale relative to
workflow-backend's own data.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Tuple

logger = logging.getLogger(__name__)

_TTL_SECONDS = 30.0
_cache: Dict[str, Tuple[float, str]] = {}


async def get_workspace_organization_id(
    workspace_id: str, *, user_id: str | None = None, org_id: str | None = None
) -> str:
    """Return the organization_id owning workspace_id, or "" if unknown/unavailable.

    Returns "" (permissive — callers should treat this as "skip org-scoped
    checks") when workflow-backend is unconfigured, the workspace isn't
    found, or the lookup errors.
    """
    from src.services.workflow_backend_client import check_workflow_available
    from src.services.workflow_backend_client import get_workspace_organization_id as _fetch

    if not workspace_id or not check_workflow_available():
        return ""

    cached = _cache.get(workspace_id)
    if cached and (time.monotonic() - cached[0]) < _TTL_SECONDS:
        return cached[1]

    try:
        fetched_org_id = await _fetch(workspace_id, user_id=user_id, org_id=org_id)
    except Exception:
        logger.exception("workflow-backend org_id lookup failed for workspace %s", workspace_id)
        return ""

    fetched_org_id = fetched_org_id or ""
    _cache[workspace_id] = (time.monotonic(), fetched_org_id)
    return fetched_org_id
