"""Trusted-identity extraction for requests arriving via the BFF gateway.

The workflow-bff authenticates the browser session and forwards the resolved
identity as headers (``X-User-Id`` / ``X-Org-Id``), gated by a shared service
token presented as ``Authorization: Bearer <token>``. src trusts
those headers as authoritative — it never sees the browser cookie and must not
trust a ``user_id`` supplied in the request body.

When ``GATEWAY_SERVICE_TOKEN`` is unset (local dev / direct testing) the token
check is skipped so the gateway can still be exercised without the BFF in front
of it.
"""

from __future__ import annotations

import hmac
import os

from fastapi import HTTPException, Request
from pydantic import BaseModel

from src.services import platform_role_client


class Identity(BaseModel):
    """Caller identity resolved from BFF-injected request headers."""

    user_id: str = ""
    org_id: str = ""


def _check_service_token(request: Request) -> None:
    """Validate the shared service token when one is configured."""
    expected = os.environ.get("GATEWAY_SERVICE_TOKEN", "")
    if not expected:
        return  # auth disabled: trust the network (local dev)

    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    token = header[len(prefix) :] if header.startswith(prefix) else ""
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing service token.")


def require_identity(request: Request) -> Identity:
    """FastAPI dependency: enforce the service token and read trusted identity."""
    _check_service_token(request)
    return Identity(
        user_id=request.headers.get("X-User-Id", "").strip(),
        org_id=request.headers.get("X-Org-Id", "").strip(),
    )


def require_service_token(request: Request) -> None:
    """FastAPI dependency for pure service-to-service calls (no user identity) —
    validates the shared service token only. Used by internal endpoints that other
    backend services (e.g. workflow-backend) call directly."""
    _check_service_token(request)


async def require_platform_admin(request: Request) -> Identity:
    """FastAPI dependency: enforce service token, read identity, then verify platform_admin role.

    Calls ``require_identity`` first (validates GATEWAY_SERVICE_TOKEN and reads
    X-User-Id / X-Org-Id), then checks that the user holds the ``platform_admin``
    role via user-service's /internal/users/:userId/platform-roles/check endpoint.

    **Fail-closed**: any network error, timeout, or unset USER_SERVICE_URL raises
    403 — this is a deliberate deviation from cost_client.py's fail-open convention.
    For availability concerns (quota, cost) failing open is acceptable; for
    authorization it is not.
    """
    identity = require_identity(request)
    if not identity.user_id:
        raise HTTPException(status_code=401, detail="Missing user identity.")

    has_role = await platform_role_client.has_role(identity.user_id, "platform_admin")
    if not has_role:
        raise HTTPException(status_code=403, detail="Platform admin role required.")

    return identity
