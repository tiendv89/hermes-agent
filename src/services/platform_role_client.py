"""HTTP client for user-service platform-role checks.

hermes-agent calls user-service directly, server-to-server, to verify whether
the requesting user holds a given platform role (e.g. ``platform_admin``).

Configuration (env vars) — same as cost_client.py:
  USER_SERVICE_URL    Base URL of user-service, e.g. http://user-service:8082.
  USER_SERVICE_TOKEN  Bearer token for service-to-service auth (INTERNAL_TOKEN).

**IMPORTANT — Fail-closed, not fail-open.**

This client deliberately diverges from cost_client.py's fail-open convention.
For availability concerns (quota checks, cost emission) hermes-agent defaults to
allowing actions when user-service is unreachable — losing a cost event is
preferable to blocking a legitimate user turn.

Authorization checks are different: silently allowing admin actions when the
role-check call fails would be a security hole. Any error, timeout, or unset
USER_SERVICE_URL must therefore return ``False`` (not ``True``), causing callers
to raise 403. The comment ``# fail-closed`` is added at every relevant branch to
make this deviation from cost_client.py's pattern obvious and unsurprisable.
"""

from __future__ import annotations

import logging
import os
from typing import Dict

import aiohttp

logger = logging.getLogger(__name__)

# Timeout for the platform-roles check.  Keep it short: admin endpoints are low-
# frequency and a hung user-service should not block the request indefinitely.
_TIMEOUT_SECONDS = 5


def _base_url() -> str:
    return os.environ.get("USER_SERVICE_URL", "").rstrip("/")


def _headers() -> Dict[str, str]:
    token = os.environ.get("USER_SERVICE_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


async def has_role(user_id: str, role: str) -> bool:
    """Check whether *user_id* holds *role* according to user-service.

    Returns ``False`` (fail-closed) on any error, including:
    - USER_SERVICE_URL not set
    - Network error or timeout
    - Non-200 HTTP status (including 404 for unknown user)
    - Unexpected response body

    Callers translate ``False`` to 403. There is intentionally no "allow on
    error" fallback here — see module docstring.
    """
    base_url = _base_url()
    if not base_url:
        # fail-closed: no URL configured → deny
        logger.warning(
            "platform_role_client: USER_SERVICE_URL not set — denying role check for %s (fail-closed)",
            user_id,
        )
        return False

    url = f"{base_url}/internal/users/{user_id}/platform-roles/check"
    params = {"role": role}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "platform_role_client: role check %s -> HTTP %s (fail-closed)",
                        url,
                        resp.status,
                    )
                    return False  # fail-closed
                raw = await resp.json()
                # user-service wraps responses as {"success", "data"}; unwrap
                # before reading has_role, tolerating a raw (unenveloped) body.
                body = raw.get("data") if isinstance(raw, dict) and isinstance(raw.get("data"), dict) else raw
                return bool(body.get("has_role", False)) if isinstance(body, dict) else False
    except Exception:
        logger.exception(
            "platform_role_client: role check failed for user %s (fail-closed)", user_id
        )
        return False  # fail-closed
