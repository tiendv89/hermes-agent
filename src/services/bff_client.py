"""HTTP client for workflow-bff cost/quota endpoints.

hermes-agent calls workflow-bff server-to-server:
  - pre-turn: GET /sessions/:id/quota/check  (quota guard — reject before Claude call)
  - post-turn: POST /sessions/:id/turn-costs  (emit token usage + cost event)

Configuration (env vars):
  WORKFLOW_BFF_URL          Base URL of workflow-bff, e.g. http://workflow-bff:8080.
                            If unset, quota checks fail open and cost emission is skipped
                            (permissive — for local dev / direct testing without the stack).
  WORKFLOW_BFF_SERVICE_TOKEN  Optional Bearer token for service-to-service auth.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

import aiohttp

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return os.environ.get("WORKFLOW_BFF_URL", "").rstrip("/")


def _headers() -> Dict[str, str]:
    token = os.environ.get("WORKFLOW_BFF_SERVICE_TOKEN", "")
    return {"Authorization": f"Bearer {token}"} if token else {}


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
    """Call the BFF quota-check endpoint before a turn.

    Returns a fail-open QuotaCheckResult when WORKFLOW_BFF_URL is unset or
    on any network/parse error — the guard must never block a turn due to
    BFF unavailability.
    """
    base_url = _base_url()
    if not base_url:
        logger.debug(
            "bff_client: WORKFLOW_BFF_URL not set — quota check skipped (fail-open) for %s",
            session_id,
        )
        return QuotaCheckResult.fail_open()

    url = f"{base_url}/sessions/{session_id}/quota/check"
    params: Dict[str, str] = {"user_id": user_id}
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
                        "bff_client: quota check %s -> %s (fail-open)", url, resp.status
                    )
                    return QuotaCheckResult.fail_open()
                body = await resp.json()
                return QuotaCheckResult.from_dict(body)
    except Exception:
        logger.exception(
            "bff_client: quota check failed for session %s (fail-open)", session_id
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
    org_id: Optional[str] = None,
) -> None:
    """POST a cost event to the BFF after a turn completes or is stopped.

    On stop, call with stopped=True and the accumulated partial counts.
    Silently skips when WORKFLOW_BFF_URL is unset. Errors are logged but never
    propagated — cost emission must not fail the turn.
    """
    base_url = _base_url()
    if not base_url:
        logger.debug(
            "bff_client: WORKFLOW_BFF_URL not set — cost emission skipped for %s",
            session_id,
        )
        return

    url = f"{base_url}/sessions/{session_id}/turn-costs"
    payload: Dict[str, Any] = {
        "user_id": user_id,
        "model": model,
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
                        "bff_client: turn-cost emission %s -> %s: %s",
                        url,
                        resp.status,
                        text[:200],
                    )
    except Exception:
        logger.exception(
            "bff_client: turn-cost emission failed for session %s", session_id
        )
