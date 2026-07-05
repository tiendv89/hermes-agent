"""Thin async HTTP client for the notification-service producer API.

Fire-and-forget design: callers use schedule_notification / schedule_notifications_bulk
which create background asyncio tasks, so the message-save path is never blocked by
notification-service latency or errors.

Configuration (env vars — all optional; if NOTIFICATION_SERVICE_URL is absent, all
calls are silently no-ops so hermes-agent runs normally without the service):
  NOTIFICATION_SERVICE_URL    Base URL, e.g. http://notification-service:8080
  NOTIFICATION_SERVICE_TOKEN  Service-to-service Bearer token
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)


def _base_url() -> str:
    return os.environ.get("NOTIFICATION_SERVICE_URL", "").rstrip("/")


def _token() -> str:
    return os.environ.get("NOTIFICATION_SERVICE_TOKEN", "")


async def _post(url: str, payload: Any) -> None:
    """POST JSON to notification-service; swallow all errors."""
    token = _token()
    headers: Dict[str, str] = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with aiohttp.ClientSession() as http:
            async with http.post(
                url,
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status not in (200, 201, 204):
                    logger.warning(
                        "notification-service %s returned HTTP %s", url, resp.status
                    )
    except Exception:
        logger.exception("notification-service call failed: %s", url)


def schedule_notification(payload: Dict[str, Any]) -> None:
    """Fire-and-forget: emit a single notification without blocking the caller."""
    base = _base_url()
    if not base:
        return
    _schedule(_post(f"{base}/internal/notifications", payload))


def schedule_notifications_bulk(payloads: List[Dict[str, Any]]) -> None:
    """Fire-and-forget: emit N notifications in one bulk call without blocking."""
    if not payloads:
        return
    base = _base_url()
    if not base:
        return
    if len(payloads) == 1:
        _schedule(_post(f"{base}/internal/notifications", payloads[0]))
        return
    _schedule(_post(f"{base}/internal/notifications/bulk", payloads))


def _schedule(coro: Any) -> None:
    """Schedule *coro* as a background task on the running event loop.

    If no event loop is running (e.g. during unit tests that call store
    functions synchronously) the notification is silently dropped — it is
    always best-effort and must never raise.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(coro)
    except RuntimeError:
        # No running event loop — safe to ignore.
        pass


def build_mention_payload(
    workspace_id: str,
    user_id: str,
    message_id: int,
    session_id: str,
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a ``mention`` notification payload."""
    payload: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "category": "mention",
        "source_type": "message",
        "source_id": str(message_id),
        "summary": "You were mentioned in a message",
        "link": f"/sessions/{session_id}",
    }
    if actor_user_id:
        payload["actor_user_id"] = actor_user_id
    return payload


def build_channel_message_payload(
    workspace_id: str,
    user_id: str,
    message_id: int,
    session_id: str,
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a ``channel_message`` notification payload."""
    payload: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "category": "channel_message",
        "source_type": "message",
        "source_id": str(message_id),
        "summary": "New message in a channel you follow",
        "link": f"/sessions/{session_id}",
    }
    if actor_user_id:
        payload["actor_user_id"] = actor_user_id
    return payload


def build_dm_payload(
    workspace_id: str,
    user_id: str,
    message_id: int,
    session_id: str,
    actor_user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a ``dm`` notification payload."""
    payload: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "category": "dm",
        "source_type": "message",
        "source_id": str(message_id),
        "summary": "You have a new direct message",
        "link": f"/sessions/{session_id}",
    }
    if actor_user_id:
        payload["actor_user_id"] = actor_user_id
    return payload
