"""JWT identity extraction for IDE coding-agent requests.

The IDE extension authenticates via OAuth device flow and sends a Bearer JWT
on every request.  This module validates that JWT using a shared secret
(``CODING_JWT_SECRET``, HS256) that must match what user-service issued.

When ``CODING_JWT_SECRET`` is unset (local dev / direct testing) the token
check is skipped so the endpoint can still be exercised.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass, field
from typing import List

from fastapi import Header, HTTPException


@dataclass
class CodingIdentity:
    """Caller identity resolved from a validated coding JWT."""

    user_id: str = ""
    org_id: str = ""
    accessible_workspace_ids: List[str] = field(default_factory=list)


def _base64url_decode(data: str) -> bytes:
    """Decode a base64url-encoded string (no padding required)."""
    rem = len(data) % 4
    if rem:
        data += "=" * (4 - rem)
    return base64.urlsafe_b64decode(data)


def _verify_coding_jwt(token: str) -> dict:
    """Verify and decode a coding JWT signed with HS256.

    Returns the decoded payload dict on success.

    Raises:
        HTTPException(401) on any validation failure.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise HTTPException(status_code=401, detail="Invalid token format")

    header_b64, payload_b64, signature_b64 = parts
    secret = os.environ.get("CODING_JWT_SECRET", "")

    # Verify signature when a secret is configured.
    if secret:
        signing_input = f"{header_b64}.{payload_b64}"
        expected_sig = (
            base64.urlsafe_b64encode(
                hmac.new(
                    secret.encode("utf-8"),
                    signing_input.encode("utf-8"),
                    hashlib.sha256,
                ).digest()
            )
            .rstrip(b"=")
            .decode("utf-8")
        )
        if not hmac.compare_digest(signature_b64, expected_sig):
            raise HTTPException(status_code=401, detail="Invalid token signature")

    # Decode payload.
    try:
        payload_bytes = _base64url_decode(payload_b64)
        payload = json.loads(payload_bytes)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=401, detail="Invalid token payload")

    # Check expiration.
    exp = payload.get("exp")
    if exp is not None and exp < time.time():
        raise HTTPException(status_code=401, detail="Token has expired")

    return payload


def require_coding_identity(
    authorization: str = Header(..., alias="Authorization"),
) -> CodingIdentity:
    """FastAPI dependency: validate the coding JWT and return caller identity.

    The JWT contains:

    * ``sub`` — user_id (UUID)
    * ``org`` — org_id (UUID)
    * ``workspaces`` — list of accessible workspace_ids
    * ``exp`` — expiration timestamp (Unix seconds)

    When ``CODING_JWT_SECRET`` is unset the signature check is skipped
    (convenience for local dev / direct testing without user-service).
    """
    if not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required")

    token = authorization[7:]

    secret = os.environ.get("CODING_JWT_SECRET", "")
    if not secret:
        # Auth disabled — trust the unsigned token payload (local dev).
        try:
            parts = token.split(".")
            if len(parts) == 3:
                payload = json.loads(_base64url_decode(parts[1]))
            elif len(parts) == 1:
                payload = json.loads(base64.urlsafe_b64decode(token + "=="))
            else:
                payload = {}
        except Exception:
            payload = {}
        return CodingIdentity(
            user_id=payload.get("sub", ""),
            org_id=payload.get("org", ""),
            accessible_workspace_ids=payload.get("workspaces", []),
        )

    try:
        payload = _verify_coding_jwt(token)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return CodingIdentity(
        user_id=payload.get("sub", ""),
        org_id=payload.get("org", ""),
        accessible_workspace_ids=payload.get("workspaces", []),
    )
