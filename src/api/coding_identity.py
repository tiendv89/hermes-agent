"""Trusted-identity extraction for IDE coding-agent requests.

The IDE extensions (VS Code, JetBrains) authenticate via OAuth device flow
and hold their own JWT, but workflow-bff — not this service — verifies it:
the BFF is the single place that knows the device-flow signing secret, and
on a coding_jwt_auth route it validates the bearer JWT itself and forwards
the resolved identity as headers (``X-User-Id`` / ``X-Org-Id`` /
``X-Accessible-Org-Ids``), gated by the same shared service token
(``GATEWAY_SERVICE_TOKEN``) the workflow profile's identity.py already uses
for the browser/session path. This module just layers workspace-id parsing
on top of that same trusted-header mechanism — it never sees or verifies
the JWT itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List

from fastapi import Request

from src.api.identity import require_identity


@dataclass
class CodingIdentity:
    """Caller identity resolved from BFF-injected request headers."""

    user_id: str = ""
    org_id: str = ""
    accessible_workspace_ids: List[str] = field(default_factory=list)


def require_coding_identity(request: Request) -> CodingIdentity:
    """FastAPI dependency: enforce the service token and read trusted identity.

    ``X-Accessible-Org-Ids`` is comma-separated (matching the header workflow-bff
    already injects for the browser path — see constant.HeaderAccessibleOrgIDs
    on the Go side); despite the field name (a holdover from the JWT-based
    predecessor of this module), these are organization ids, not workflow
    workspace ids — see user-service's deviceauth.CodingClaims doc comment.
    """
    identity = require_identity(request)
    raw = request.headers.get("X-Accessible-Org-Ids", "")
    accessible = [o.strip() for o in raw.split(",") if o.strip()]
    return CodingIdentity(
        user_id=identity.user_id,
        org_id=identity.org_id,
        accessible_workspace_ids=accessible,
    )
