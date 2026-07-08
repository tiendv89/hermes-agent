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

# asyncio.Task objects are only weakly referenced by the event loop; with no
# other reference, a task can be garbage-collected mid-flight (before its HTTP
# call completes), silently dropping the notification. Keep a strong
# reference here until each task finishes. See:
# https://docs.python.org/3/library/asyncio-task.html#asyncio.create_task
_background_tasks: set[asyncio.Task[None]] = set()


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


def _publish_realtime(payload: Dict[str, Any]) -> None:
    """Push a notification event to the recipient's live SSE subscribers
    (GET /api/v1/notifications/stream), independent of and not blocked by the
    notification-service HTTP call below — so a toast can render even if
    notification-service is slow/unreachable, though the persisted row won't
    exist until that call succeeds.
    """
    try:
        from src.realtime.user_bus import get_user_bus

        user_id = payload.get("user_id")
        if user_id:
            get_user_bus().publish(
                user_id, {"event": "notification.created", "data": payload}
            )
    except Exception:
        logger.exception("failed to publish realtime notification event")


def schedule_notification(payload: Dict[str, Any]) -> None:
    """Fire-and-forget: emit a single notification without blocking the caller."""
    _publish_realtime(payload)
    base = _base_url()
    if not base:
        return
    _schedule(_post(f"{base}/internal/notifications", payload))


def schedule_notifications_bulk(payloads: List[Dict[str, Any]]) -> None:
    """Fire-and-forget: emit N notifications in one bulk call without blocking."""
    if not payloads:
        return
    for payload in payloads:
        _publish_realtime(payload)
    base = _base_url()
    if not base:
        return
    if len(payloads) == 1:
        _schedule(_post(f"{base}/internal/notifications", payloads[0]))
        return
    _schedule(_post(f"{base}/internal/notifications/bulk", payloads))


def schedule_background(coro: Any) -> None:
    """Public wrapper around _schedule for callers outside this module (e.g.
    approval_notifications) that need the same fire-and-forget, GC-safe
    strong-reference behavior for an arbitrary coroutine."""
    _schedule(coro)


def _schedule(coro: Any) -> None:
    """Schedule *coro* as a background task on the running event loop.

    If no event loop is running (e.g. during unit tests that call store
    functions synchronously) the notification is silently dropped — it is
    always best-effort and must never raise.
    """
    try:
        loop = asyncio.get_running_loop()
        task = loop.create_task(coro)
    except RuntimeError:
        # No running event loop — safe to ignore.
        return
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


_PREVIEW_MAX_LEN = 140


def _truncate(text: str, max_len: int = _PREVIEW_MAX_LEN) -> str:
    """Collapse whitespace/newlines and clip to max_len with an ellipsis."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= max_len:
        return collapsed
    return collapsed[: max_len - 1].rstrip() + "…"


_REPLY_VERB: Dict[str, str] = {
    "thread": "replied to a thread",
    "message": "replied to a message",
}


def _compose_summary(actor_name: Optional[str], content: str, reply_kind: Optional[str] = None) -> str:
    """Build a preview: "<actor>: <content>" for a plain post, or "<actor> replied to
    a thread: <content>" / "<actor> replied to a message: <content>" for a reply —
    otherwise a reply is indistinguishable from an ordinary channel post in the
    activity feed, even though the two land in very different places in the UI.

    `reply_kind` is "thread" for a message posted through the thread side panel
    (thread_root_id set), "message" for an inline quoted reply in the main
    transcript (reply_to_message_id set, no thread_root_id), or None for a plain,
    non-reply message.

    <content> is passed through unmodified (aside from truncation) so the FE
    can run it through the same @mention-highlighting renderer used for
    regular chat messages. Channel/feature context is NOT embedded here —
    the payload carries session_id/feature_id as structured fields and the FE
    resolves display names (channel title, feature slug) by looking those up
    against hermes-agent's own session/feature APIs, the same way the /chat
    and /feature pages already do.
    """
    who = actor_name or "Someone"
    verb = _REPLY_VERB.get(reply_kind or "", "")
    prefix = f"{who} {verb}" if verb else who
    return f"{prefix}: {_truncate(content)}"


def _channel_link(session_id: str, feature_id: Optional[str]) -> str:
    """Feature-scoped channels don't appear in the regular Chat sidebar — they
    only live inside that feature's Feature IDE view — so route there instead
    of the generic /chat/{id} used for workspace-level channels and DMs."""
    if feature_id:
        return f"/feature/{feature_id}?channel={session_id}"
    return f"/chat/{session_id}"


def build_mention_payload(
    workspace_id: str,
    user_id: str,
    message_id: int,
    session_id: str,
    content: str,
    actor_user_id: Optional[str] = None,
    actor_name: Optional[str] = None,
    feature_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a ``mention`` notification payload."""
    payload: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "category": "mention",
        "source_type": "message",
        "source_id": str(message_id),
        "session_id": session_id,
        "summary": _compose_summary(actor_name, content),
        "link": _channel_link(session_id, feature_id),
    }
    if actor_user_id:
        payload["actor_user_id"] = actor_user_id
    if feature_id:
        payload["feature_id"] = feature_id
    return payload


def build_channel_message_payload(
    workspace_id: str,
    user_id: str,
    message_id: int,
    session_id: str,
    content: str,
    actor_user_id: Optional[str] = None,
    actor_name: Optional[str] = None,
    feature_id: Optional[str] = None,
    reply_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a ``channel_message`` notification payload. `reply_kind`
    ("thread" | "message" | None, see _compose_summary) only changes the summary
    wording — the category stays ``channel_message`` since notification-service's
    category allow-list has no dedicated reply value."""
    payload: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "category": "channel_message",
        "source_type": "message",
        "source_id": str(message_id),
        "session_id": session_id,
        "summary": _compose_summary(actor_name, content, reply_kind=reply_kind),
        "link": _channel_link(session_id, feature_id),
    }
    if actor_user_id:
        payload["actor_user_id"] = actor_user_id
    if feature_id:
        payload["feature_id"] = feature_id
    return payload


def build_dm_payload(
    workspace_id: str,
    user_id: str,
    message_id: int,
    session_id: str,
    content: str,
    actor_user_id: Optional[str] = None,
    actor_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a ``dm`` notification payload."""
    payload: Dict[str, Any] = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "category": "dm",
        "source_type": "message",
        "source_id": str(message_id),
        "session_id": session_id,
        "summary": _compose_summary(actor_name, content),
        "link": f"/chat/{session_id}",
    }
    if actor_user_id:
        payload["actor_user_id"] = actor_user_id
    return payload


# Maps a stage-transition's `stage` to the notification category it produces
# on approval. "handoff" has no corresponding category — approving it isn't
# one of the notified events.
STAGE_CATEGORY: Dict[str, str] = {
    "product_spec": "spec_approved",
    "technical_design": "design_approved",
    "tasks": "tasks_approved",
}

STAGE_DESCRIPTION: Dict[str, str] = {
    "product_spec": "the product spec",
    "technical_design": "the technical design",
    "tasks": "the task breakdown",
}


def build_approval_payload(
    workspace_id: str,
    user_id: str,
    feature_id: str,
    stage: str,
    actor_user_id: Optional[str] = None,
    actor_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Construct a stage-approval notification payload (spec/design/tasks
    approved). Raises KeyError if stage isn't one of the notified stages —
    callers should check `stage in STAGE_CATEGORY` first."""
    who = actor_name or "Someone"
    description = STAGE_DESCRIPTION[stage]
    return {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "category": STAGE_CATEGORY[stage],
        "source_type": "feature",
        "source_id": feature_id,
        "feature_id": feature_id,
        "summary": f"{who} approved {description}",
        "link": f"/feature/{feature_id}",
        **({"actor_user_id": actor_user_id} if actor_user_id else {}),
    }
