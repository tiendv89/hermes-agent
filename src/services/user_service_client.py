"""HTTP client for user-service workspace-role lookups.

hermes-agent calls user-service server-to-server to confirm the caller is a
workspace admin before allowing channel deletion (§3.6 / T4 / T5).

Configuration (env vars, all optional if running without the full stack):
  USER_SERVICE_URL    Base URL of user-service, e.g. http://user-service:8080.
                      If unset, admin checks are bypassed (permissive — for
                      local dev / direct testing without the BFF stack).
  USER_SERVICE_TOKEN  Optional Bearer token for service-to-service auth.

Endpoint contract (from T5 implementation):
  GET {USER_SERVICE_URL}/api/v1/workspaces/{workspace_id}/members/{user_id}
  → 200  {"user_id": "...", "workspace_id": "...", "role": "admin"|"member"|...}
  → 404  not a member
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)

_DEFAULT_ADMIN_ROLES = frozenset({"admin", "owner"})


class UserServiceError(Exception):
    """Raised when user-service returns an unexpected response."""


async def get_workspace_role(
    workspace_id: str,
    user_id: str,
) -> Optional[str]:
    """Return the caller's workspace role string, or None if not a member.

    Returns None also when USER_SERVICE_URL is not set (permissive dev mode).
    Raises :class:`UserServiceError` on HTTP errors other than 404.
    """
    base_url = os.environ.get("USER_SERVICE_URL", "").rstrip("/")
    if not base_url:
        logger.debug(
            "USER_SERVICE_URL not set — skipping workspace-role check for %s/%s",
            workspace_id,
            user_id,
        )
        return None

    token = os.environ.get("USER_SERVICE_TOKEN", "")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{base_url}/api/v1/workspaces/{workspace_id}/members/{user_id}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                text = await resp.text()
                raise UserServiceError(
                    f"user-service returned {resp.status} for {url}: {text[:200]}"
                )
            data = await resp.json()
            return data.get("role")


async def is_workspace_admin(
    workspace_id: str,
    user_id: str,
    *,
    admin_roles: frozenset = _DEFAULT_ADMIN_ROLES,
) -> bool:
    """Return True if user_id is an admin/owner of workspace_id.

    When USER_SERVICE_URL is unset (permissive dev mode), returns True so
    local direct-call tests are not blocked by the admin gate.
    """
    base_url = os.environ.get("USER_SERVICE_URL", "")
    if not base_url:
        logger.debug(
            "USER_SERVICE_URL not set — granting admin for %s/%s (dev mode)",
            workspace_id,
            user_id,
        )
        return True

    role = await get_workspace_role(workspace_id, user_id)
    return role in admin_roles
