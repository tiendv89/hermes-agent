"""HTTP client for user-service org-role/org-member lookups.

hermes-agent calls user-service server-to-server to confirm the caller is an
org admin before allowing channel deletion, and to resolve DM eligibility and
@mention candidates against the org roster (§3.6 / T4 / T5, reworked when
user-service's workspace_memberships table was removed — per-workspace roles
never diverged from org roles in practice, so all membership/permission
checks are now org-scoped).

Configuration (env vars, all optional if running without the full stack):
  USER_SERVICE_URL    Base URL of user-service, e.g. http://user-service:8080.
                      If unset, admin checks are bypassed (permissive — for
                      local dev / direct testing without the BFF stack).
  USER_SERVICE_TOKEN  Optional Bearer token for service-to-service auth.

Endpoint contracts:
  GET {USER_SERVICE_URL}/internal/org-role?org_id=<uuid>&user_id=<uuid>
  → 200  {"organization_id": "...", "user_id": "...", "role": "admin"|"member"|...}
  → 404  not a member

  GET {USER_SERVICE_URL}/internal/orgs/{org_id}/members
  → 200  {"members": [{"user_id", "display_name", "email", "avatar_url", "role"}, ...]}
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

_DEFAULT_ADMIN_ROLES = frozenset({"admin", "owner"})

# Short TTL cache of org member directories so message-author resolution
# doesn't hit user-service on every transcript fetch / live message.
_MEMBERS_TTL_SECONDS = 30.0
_members_cache: Dict[str, tuple[float, Dict[str, Dict[str, Any]]]] = {}


class UserServiceError(Exception):
    """Raised when user-service returns an unexpected response."""


async def list_org_members(org_id: str) -> Dict[str, Dict[str, Any]]:
    """Return ``{user_id: {display_name, email, avatar_url, role}}`` for an org.

    Calls user-service's internal org-member directory (service-token auth).
    Results are cached briefly. Returns ``{}`` when USER_SERVICE_URL is unset
    (dev mode), org_id is empty, or on any error — callers degrade gracefully
    to id-only attribution.
    """
    if not org_id:
        return {}

    cached = _members_cache.get(org_id)
    if cached and (time.monotonic() - cached[0]) < _MEMBERS_TTL_SECONDS:
        return cached[1]

    base_url = os.environ.get("USER_SERVICE_URL", "").rstrip("/")
    if not base_url:
        return {}

    token = os.environ.get("USER_SERVICE_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{base_url}/internal/orgs/{org_id}/members"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    logger.warning("user-service org members lookup %s -> %s", url, resp.status)
                    return {}
                body = await resp.json()
    except Exception:
        logger.exception("user-service org members lookup failed for %s", org_id)
        return {}

    # RespondOK wraps as {"success": true, "data": {"members": [...]}}.
    container = body.get("data", body) if isinstance(body, dict) else {}
    members = container.get("members", []) if isinstance(container, dict) else []
    out: Dict[str, Dict[str, Any]] = {}
    for m in members or []:
        uid = m.get("user_id")
        if uid:
            out[uid] = m
    _members_cache[org_id] = (time.monotonic(), out)
    return out


# Short TTL cache of individual user profiles (for author resolution).
_users_cache: Dict[str, tuple[float, Dict[str, Any]]] = {}


async def list_users_by_ids(user_ids: list[str]) -> Dict[str, Dict[str, Any]]:
    """Return ``{user_id: {display_name, email, avatar_url}}`` for the given ids.

    Resolves any user regardless of org membership (unlike list_org_members).
    Cached per-id; returns ``{}`` when USER_SERVICE_URL is unset or on error.
    """
    ids = [uid for uid in dict.fromkeys(user_ids) if uid]
    if not ids:
        return {}

    now = time.monotonic()
    resolved: Dict[str, Dict[str, Any]] = {}
    missing: list[str] = []
    for uid in ids:
        cached = _users_cache.get(uid)
        if cached and (now - cached[0]) < _MEMBERS_TTL_SECONDS:
            resolved[uid] = cached[1]
        else:
            missing.append(uid)

    if not missing:
        return resolved

    base_url = os.environ.get("USER_SERVICE_URL", "").rstrip("/")
    if not base_url:
        return resolved

    token = os.environ.get("USER_SERVICE_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    url = f"{base_url}/internal/users?ids={','.join(missing)}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status != 200:
                    logger.warning("user-service users lookup %s -> %s", url, resp.status)
                    return resolved
                body = await resp.json()
    except Exception:
        logger.exception("user-service users lookup failed")
        return resolved

    container = body.get("data", body) if isinstance(body, dict) else {}
    users = container.get("users", []) if isinstance(container, dict) else []
    for u in users or []:
        uid = u.get("user_id")
        if uid:
            _users_cache[uid] = (now, u)
            resolved[uid] = u
    return resolved


async def get_org_role(
    org_id: str,
    user_id: str,
) -> Optional[str]:
    """Return the caller's org role string, or None if not a member.

    Returns None also when USER_SERVICE_URL is not set (permissive dev mode).
    Raises :class:`UserServiceError` on HTTP errors other than 404.
    """
    base_url = os.environ.get("USER_SERVICE_URL", "").rstrip("/")
    if not base_url:
        logger.debug(
            "USER_SERVICE_URL not set — skipping org-role check for %s/%s",
            org_id,
            user_id,
        )
        return None

    token = os.environ.get("USER_SERVICE_TOKEN", "")
    headers = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    url = f"{base_url}/internal/org-role"
    params = {"org_id": org_id, "user_id": user_id}
    async with aiohttp.ClientSession() as session:
        async with session.get(
            url, params=params, headers=headers, timeout=aiohttp.ClientTimeout(total=5)
        ) as resp:
            if resp.status == 404:
                return None
            if resp.status != 200:
                text = await resp.text()
                raise UserServiceError(
                    f"user-service returned {resp.status} for {url}: {text[:200]}"
                )
            data = await resp.json()
            return data.get("role")


async def is_org_admin(
    org_id: str,
    user_id: str,
    *,
    admin_roles: frozenset = _DEFAULT_ADMIN_ROLES,
) -> bool:
    """Return True if user_id is an admin/owner of org_id.

    When USER_SERVICE_URL is unset (permissive dev mode), returns True so
    local direct-call tests are not blocked by the admin gate.
    """
    base_url = os.environ.get("USER_SERVICE_URL", "")
    if not base_url:
        logger.debug(
            "USER_SERVICE_URL not set — granting admin for %s/%s (dev mode)",
            org_id,
            user_id,
        )
        return True

    role = await get_org_role(org_id, user_id)
    return role in admin_roles
