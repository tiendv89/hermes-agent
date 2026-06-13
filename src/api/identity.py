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
    token = header[len(prefix):] if header.startswith(prefix) else ""
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="Invalid or missing service token.")


def require_identity(request: Request) -> Identity:
    """FastAPI dependency: enforce the service token and read trusted identity."""
    _check_service_token(request)
    return Identity(
        user_id=request.headers.get("X-User-Id", "").strip(),
        org_id=request.headers.get("X-Org-Id", "").strip(),
    )
