"""HTTP client for user-service cost/quota endpoints.

hermes-agent calls user-service directly, server-to-server (no BFF hop — the BFF
only adds value for browser/cookie callers, which hermes is not):
  - pre-turn:  GET  {USER_SERVICE_URL}/internal/users/{user_id}/quota/check
               (quota guard — reject before the Claude call)
  - post-turn: POST {USER_SERVICE_URL}/internal/turn-costs
               (emit token usage + cost event)

Configuration (env vars):
  USER_SERVICE_URL    Base URL of user-service, e.g. http://user-service:8082.
                      If unset, quota checks fail open and cost emission is
                      skipped (permissive — for local dev / direct testing
                      without the stack).
  USER_SERVICE_TOKEN  Bearer token for service-to-service auth (shared
                      INTERNAL_TOKEN). Required by user-service's /internal/*.

Contract notes:
  - user-service types turn_cost.session_id / turn_id as UUID. hermes session
    ids are opaque "sess_<hex>" strings, so _as_uuid() maps them deterministically
    (same input -> same UUID). run_id is already a uuid4 hex and parses as-is.
  - user-service requires model_id, source_type, source_label on turn-costs.
  - user-service wraps success bodies as {"success": true, "data": {...}};
    _unwrap() tolerates both enveloped and raw responses.
"""

from __future__ import annotations

import logging
import os
import uuid as _uuid
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Stable namespace for deriving a UUID from hermes' opaque "sess_..." ids so the
# same session/turn id always maps to the same UUID in user-service.
_ID_NAMESPACE = _uuid.UUID("a4e1c0de-0000-4000-8000-000000000001")


def _base_url() -> str:
    return os.environ.get("USER_SERVICE_URL", "").rstrip("/")


def _headers() -> Dict[str, str]:
    token = os.environ.get("USER_SERVICE_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _as_uuid(value: str, *, salt: str = "") -> str:
    """Return a canonical UUID string for *value*.

    If *value* already parses as a UUID (e.g. a uuid4 hex run_id), it is used
    verbatim; otherwise a deterministic UUIDv5 is derived so opaque ids like
    "sess_abc123" map stably to a UUID.
    """
    try:
        return str(_uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return str(_uuid.uuid5(_ID_NAMESPACE, f"{salt}{value or ''}"))


def _unwrap(body: Any) -> Dict[str, Any]:
    """Unwrap user-service's {"success", "data"} envelope, tolerating raw bodies."""
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict):
            return data
        return body
    return {}


class QuotaCheckResult:
    """Result of a pre-turn quota check."""

    __slots__ = ("allowed", "reason", "resets_at", "plan_name", "daily_cap", "weekly_cap")

    def __init__(
        self,
        *,
        allowed: bool,
        reason: Optional[str] = None,
        resets_at: Optional[str] = None,
        plan_name: Optional[str] = None,
        daily_cap: Optional[int] = None,
        weekly_cap: Optional[int] = None,
    ) -> None:
        self.allowed = allowed
        self.reason = reason
        self.resets_at = resets_at
        self.plan_name = plan_name
        self.daily_cap = daily_cap
        self.weekly_cap = weekly_cap

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "QuotaCheckResult":
        return cls(
            allowed=bool(d.get("allowed", True)),
            reason=d.get("reason"),
            resets_at=d.get("resets_at"),
            plan_name=d.get("plan_name"),
            daily_cap=d.get("daily_cap"),
            weekly_cap=d.get("weekly_cap"),
        )

    @classmethod
    def fail_open(cls) -> "QuotaCheckResult":
        return cls(allowed=True)


async def check_quota(
    session_id: str,
    user_id: str,
    *,
    org_id: Optional[str] = None,
) -> QuotaCheckResult:
    """Call user-service's quota-check endpoint before a turn.

    *session_id* is kept for logging context only — user-service resolves quota
    per user (and per org for plan resolution), not per session.

    Returns a fail-open QuotaCheckResult when USER_SERVICE_URL is unset or on any
    network/parse error — the guard must never block a turn due to user-service
    unavailability.
    """
    base_url = _base_url()
    if not base_url:
        logger.debug(
            "cost_client: USER_SERVICE_URL not set — quota check skipped (fail-open) for %s",
            session_id,
        )
        return QuotaCheckResult.fail_open()

    url = f"{base_url}/internal/users/{user_id}/quota/check"
    params: Dict[str, str] = {}
    if org_id:
        params["org_id"] = org_id

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                params=params,
                headers=_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "cost_client: quota check %s -> %s (fail-open)", url, resp.status
                    )
                    return QuotaCheckResult.fail_open()
                body = await resp.json()
                return QuotaCheckResult.from_dict(_unwrap(body))
    except Exception:
        logger.exception(
            "cost_client: quota check failed for session %s (fail-open)", session_id
        )
        return QuotaCheckResult.fail_open()


async def emit_turn_cost(
    session_id: str,
    user_id: str,
    model: str,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    stopped: bool = False,
    turn_id: Optional[str] = None,
    source_type: str = "agent_chat",
    source_label: str = "",
    org_id: Optional[str] = None,
) -> None:
    """POST a cost event to user-service after a turn completes or is stopped.

    On stop, call with stopped=True and the accumulated partial counts.
    Silently skips when USER_SERVICE_URL is unset. Errors are logged but never
    propagated — cost emission must not fail the turn.

    session_id / turn_id are mapped to UUIDs (see _as_uuid) to satisfy
    user-service's UUID-typed columns. model is sent as model_id.
    """
    base_url = _base_url()
    if not base_url:
        logger.debug(
            "cost_client: USER_SERVICE_URL not set — cost emission skipped for %s",
            session_id,
        )
        return

    url = f"{base_url}/internal/turn-costs"
    payload: Dict[str, Any] = {
        "user_id": user_id,
        "session_id": _as_uuid(session_id),
        "turn_id": _as_uuid(turn_id or session_id, salt="turn:"),
        "model_id": model,
        "source_type": source_type or "agent_chat",
        # source_label is required (non-empty) by user-service; fall back to the
        # session id so a turn cost is never rejected for a missing label.
        "source_label": source_label or session_id,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_write_tokens": cache_write_tokens,
        "stopped": stopped,
    }
    if org_id:
        payload["org_id"] = org_id

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json=payload,
                headers={**_headers(), "Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status not in (200, 201, 204):
                    text = await resp.text()
                    logger.warning(
                        "cost_client: turn-cost emission %s -> %s: %s",
                        url,
                        resp.status,
                        text[:200],
                    )
    except Exception:
        logger.exception(
            "cost_client: turn-cost emission failed for session %s", session_id
        )
