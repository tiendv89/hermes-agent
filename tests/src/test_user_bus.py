"""Unit tests for src/realtime/user_bus.py — the per-user pub/sub bus behind
GET /api/v1/notifications/stream — and the publish-on-schedule wiring in
notification_client.py.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_publish_delivers_to_subscriber():
    from src.realtime.user_bus import UserBus

    bus = UserBus()
    async with bus.subscribe("user-1") as q:
        bus.publish("user-1", {"event": "notification.created", "data": {"x": 1}})
        event = await asyncio.wait_for(q.get(), timeout=1)

    assert event["data"]["x"] == 1


@pytest.mark.asyncio
async def test_publish_only_reaches_matching_user_topic():
    from src.realtime.user_bus import UserBus

    bus = UserBus()
    async with bus.subscribe("user-1") as q1, bus.subscribe("user-2") as q2:
        bus.publish("user-1", {"event": "notification.created", "data": {"for": "user-1"}})

        event = await asyncio.wait_for(q1.get(), timeout=1)
        assert event["data"]["for"] == "user-1"
        assert q2.empty()


@pytest.mark.asyncio
async def test_publish_to_no_subscribers_is_a_noop():
    from src.realtime.user_bus import UserBus

    bus = UserBus()
    # Must not raise even though nobody is subscribed.
    bus.publish("user-nobody", {"event": "notification.created", "data": {}})


def test_get_user_bus_returns_singleton():
    from src.realtime.user_bus import get_user_bus

    assert get_user_bus() is get_user_bus()


@pytest.mark.asyncio
async def test_unsubscribe_stops_delivery():
    from src.realtime.user_bus import UserBus

    bus = UserBus()
    q = asyncio.Queue()
    bus.subscribe_raw("user-1", q)
    bus.unsubscribe_raw("user-1", q)

    bus.publish("user-1", {"event": "notification.created", "data": {}})
    assert q.empty()


# ---------------------------------------------------------------------------
# notification_client publish-on-schedule wiring
# ---------------------------------------------------------------------------


def test_schedule_notification_publishes_realtime_event_even_without_url(monkeypatch):
    """Realtime push must fire regardless of NOTIFICATION_SERVICE_URL — a
    toast shouldn't depend on the REST persistence path being configured."""
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "")

    from src.services.notification_client import schedule_notification

    with patch("src.realtime.user_bus.get_user_bus") as mock_get_bus:
        schedule_notification({"user_id": "user-1", "category": "mention"})

    mock_get_bus.return_value.publish.assert_called_once_with(
        "user-1", {"event": "notification.created", "data": {"user_id": "user-1", "category": "mention"}}
    )


def test_schedule_notifications_bulk_publishes_one_event_per_payload(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    from src.services.notification_client import schedule_notifications_bulk

    payloads = [
        {"user_id": "user-1", "category": "mention"},
        {"user_id": "user-2", "category": "channel_message"},
    ]
    with patch("src.realtime.user_bus.get_user_bus") as mock_get_bus:
        schedule_notifications_bulk(payloads)

    assert mock_get_bus.return_value.publish.call_count == 2


def test_publish_realtime_swallows_errors():
    """A bus failure must never propagate into the notification-scheduling path."""
    from src.services.notification_client import _publish_realtime

    with patch("src.realtime.user_bus.get_user_bus", side_effect=RuntimeError("boom")):
        # Must not raise.
        _publish_realtime({"user_id": "user-1"})
