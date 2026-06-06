"""JWT auth stub for workflow gateway v1.

In v1 we accept all requests (pass-through). A future iteration will
validate Privy/JWT tokens from digital-factory-ui. The stub is already
wired in the router so plugging in real validation requires no routing
changes.
"""

from __future__ import annotations

from fastapi import Header
from typing import Optional


async def verify_token(authorization: Optional[str] = Header(default=None)) -> Optional[str]:
    """Extract and return the bearer token, or None if absent.

    v1: no validation performed. Returns the raw token string so downstream
    handlers can log or forward it.
    """
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token or None
