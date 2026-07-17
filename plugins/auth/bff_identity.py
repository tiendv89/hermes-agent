"""HMAC-SHA256 signing and verification for the X-BFF-Identity header.

The BFF (workflow-bff) signs outgoing proxy requests with an X-BFF-Identity
header carrying identity claims and a 60-second TTL.  Backend services
(storage-service, workflow-backend, notification-service) verify the signature
before trusting the identity fields.

hermes-agent is a service-side caller that talks to those backends directly —
not through the BFF.  This module lets hermes-agent sign its own identity
claims so backends can verify them the same way.

Header format (same as workflow-bff's signIdentityHeader):
  base64( claims_string + "." + hmac_hex )

Claims string:
  user_id=<val>,org_id=<val>,accessible_org_ids=<csv>,platform_role=<val>,exp=<unix_ts>

Configuration:
  BFF_SIGNING_KEY   Shared HMAC secret distributed to all services.
                    If unset, sign_bff_identity raises RuntimeError.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import time
from typing import Any

_CLAIMS_RE = re.compile(
    r"^user_id=(?P<user_id>[^,]*)"
    r",org_id=(?P<org_id>[^,]*)"
    r",accessible_org_ids=(?P<accessible_org_ids>.*?)"
    r",platform_role=(?P<platform_role>[^,]*)"
    r",exp=(?P<exp>\d+)$"
)


def _build_claims_str(
    user_id: str,
    org_id: str,
    accessible_org_ids: list[str],
    platform_role: str,
    exp: int,
) -> str:
    accessible = ",".join(accessible_org_ids)
    return (
        f"user_id={user_id}"
        f",org_id={org_id}"
        f",accessible_org_ids={accessible}"
        f",platform_role={platform_role}"
        f",exp={exp}"
    )


def _compute_hmac(claims_str: str, key: str) -> str:
    return hmac.new(key.encode(), claims_str.encode(), hashlib.sha256).hexdigest()


def sign_bff_identity(claims: dict[str, Any], key: str | None = None) -> str:
    """Return the X-BFF-Identity header value for the given identity claims.

    Args:
        claims: Dict with keys ``user_id``, ``org_id``, ``accessible_org_ids``
                (list[str]), ``platform_role`` (str, defaults to ""), and
                optionally ``exp`` (int unix timestamp, defaults to now+60).
        key:    HMAC signing key.  Defaults to the ``BFF_SIGNING_KEY`` env var.
                Raises RuntimeError when neither is provided.

    Returns:
        base64-encoded token suitable for the ``X-BFF-Identity`` header.
    """
    if key is None:
        key = os.environ.get("BFF_SIGNING_KEY", "")
    if not key:
        raise RuntimeError(
            "BFF_SIGNING_KEY is not set — cannot sign X-BFF-Identity header"
        )

    exp = int(claims.get("exp", time.time() + 60))
    claims_str = _build_claims_str(
        user_id=str(claims.get("user_id", "")),
        org_id=str(claims.get("org_id", "")),
        accessible_org_ids=list(claims.get("accessible_org_ids") or []),
        platform_role=str(claims.get("platform_role", "")),
        exp=exp,
    )
    mac = _compute_hmac(claims_str, key)
    token = base64.b64encode(f"{claims_str}.{mac}".encode()).decode()
    return token


def verify_bff_identity(header_value: str, key: str | None = None) -> dict[str, Any]:
    """Verify an X-BFF-Identity header and return the parsed claims.

    Args:
        header_value: The raw value of the ``X-BFF-Identity`` header.
        key:          HMAC signing key.  Defaults to ``BFF_SIGNING_KEY`` env var.

    Returns:
        Dict with keys ``user_id``, ``org_id``, ``accessible_org_ids``
        (list[str]), ``platform_role``, ``exp`` (int).

    Raises:
        ValueError:   Token is malformed, signature is invalid, or expired.
        RuntimeError: BFF_SIGNING_KEY is not available.
    """
    if key is None:
        key = os.environ.get("BFF_SIGNING_KEY", "")
    if not key:
        raise RuntimeError(
            "BFF_SIGNING_KEY is not set — cannot verify X-BFF-Identity header"
        )

    try:
        decoded = base64.b64decode(header_value.encode()).decode()
    except Exception as exc:
        raise ValueError(f"X-BFF-Identity: invalid base64: {exc}") from exc

    dot_pos = decoded.rfind(".")
    if dot_pos < 0:
        raise ValueError("X-BFF-Identity: missing '.' separator between claims and HMAC")

    claims_str = decoded[:dot_pos]
    provided_hmac = decoded[dot_pos + 1:]

    expected_mac = _compute_hmac(claims_str, key)
    if not hmac.compare_digest(expected_mac, provided_hmac):
        raise ValueError("X-BFF-Identity: signature verification failed")

    m = _CLAIMS_RE.match(claims_str)
    if not m:
        raise ValueError(f"X-BFF-Identity: claims string does not match expected format: {claims_str!r}")

    try:
        exp = int(m.group("exp"))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"X-BFF-Identity: invalid exp field: {exc}") from exc

    if exp < int(time.time()):
        raise ValueError(f"X-BFF-Identity: token expired (exp={exp})")

    accessible_raw = m.group("accessible_org_ids")
    accessible_org_ids = [o for o in accessible_raw.split(",") if o]

    return {
        "user_id": m.group("user_id"),
        "org_id": m.group("org_id"),
        "accessible_org_ids": accessible_org_ids,
        "platform_role": m.group("platform_role"),
        "exp": exp,
    }
