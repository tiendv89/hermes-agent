"""Unit tests for src/services/notification_client.py.

Covers:
- build_*_payload helpers produce correct shapes
- schedule_notification / schedule_notifications_bulk schedule tasks when a
  loop is running and NOTIFICATION_SERVICE_URL is set
- Calls are silently dropped when no NOTIFICATION_SERVICE_URL is configured
- _post swallows HTTP errors and network exceptions
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# build_*_payload tests
# ---------------------------------------------------------------------------


def test_build_mention_payload_with_actor():
    from src.services.notification_client import build_mention_payload

    p = build_mention_payload(
        workspace_id="ws-1",
        user_id="usr-2",
        message_id=42,
        session_id="sess-abc",
        content="hey @usr-2 check this out",
        actor_user_id="usr-1",
        actor_name="Duke Tran",
    )
    assert p["workspace_id"] == "ws-1"
    assert p["user_id"] == "usr-2"
    assert p["category"] == "mention"
    assert p["source_type"] == "message"
    assert p["source_id"] == "42"
    assert p["session_id"] == "sess-abc"
    assert p["link"] == "/chat/sess-abc"
    assert p["actor_user_id"] == "usr-1"
    assert p["summary"] == "Duke Tran: hey @usr-2 check this out"
    assert "feature_id" not in p


def test_build_mention_payload_without_actor():
    from src.services.notification_client import build_mention_payload

    p = build_mention_payload(
        workspace_id="ws-1",
        user_id="usr-2",
        message_id=1,
        session_id="sess-x",
        content="hi",
    )
    assert "actor_user_id" not in p
    assert p["summary"] == "Someone: hi"


def test_build_mention_payload_feature_scoped_links_into_feature_ide():
    from src.services.notification_client import build_mention_payload

    p = build_mention_payload(
        workspace_id="ws-1",
        user_id="usr-2",
        message_id=42,
        session_id="sess-abc",
        content="hi",
        feature_id="feat-1",
    )
    assert p["link"] == "/feature/feat-1?channel=sess-abc"
    assert p["feature_id"] == "feat-1"


def test_build_channel_message_payload():
    from src.services.notification_client import build_channel_message_payload

    p = build_channel_message_payload(
        workspace_id="ws-1",
        user_id="usr-3",
        message_id=99,
        session_id="chan-1",
        content="anyone around?",
        actor_user_id="usr-1",
        actor_name="Duke Tran",
    )
    assert p["category"] == "channel_message"
    assert p["user_id"] == "usr-3"
    assert p["source_id"] == "99"
    assert p["actor_user_id"] == "usr-1"
    assert p["session_id"] == "chan-1"
    assert p["summary"] == "Duke Tran: anyone around?"
    assert p["link"] == "/chat/chan-1"
    assert "feature_id" not in p


def test_build_channel_message_payload_feature_scoped_links_into_feature_ide():
    from src.services.notification_client import build_channel_message_payload

    p = build_channel_message_payload(
        workspace_id="ws-1",
        user_id="usr-3",
        message_id=99,
        session_id="chan-1",
        content="anyone around?",
        feature_id="feat-1",
    )
    assert p["link"] == "/feature/feat-1?channel=chan-1"
    assert p["feature_id"] == "feat-1"
    assert p["session_id"] == "chan-1"


def test_build_dm_payload():
    from src.services.notification_client import build_dm_payload

    p = build_dm_payload(
        workspace_id="ws-1",
        user_id="usr-4",
        message_id=7,
        session_id="dm-sess",
        content="hey there",
        actor_user_id="usr-2",
        actor_name="Pye Tran",
    )
    assert p["category"] == "dm"
    assert p["user_id"] == "usr-4"
    assert p["source_id"] == "7"
    assert p["summary"] == "Pye Tran: hey there"


def test_compose_summary_truncates_long_content():
    from src.services.notification_client import _PREVIEW_MAX_LEN, _compose_summary

    long_content = "x" * 200
    summary = _compose_summary("Duke Tran", long_content)
    assert summary.startswith("Duke Tran: ")
    preview = summary.split(": ", 1)[1]
    assert len(preview) == _PREVIEW_MAX_LEN
    assert preview.endswith("…")


def test_compose_summary_collapses_whitespace():
    from src.services.notification_client import _compose_summary

    summary = _compose_summary("Duke Tran", "hello\n\nworld   again")
    assert summary == "Duke Tran: hello world again"


# ---------------------------------------------------------------------------
# schedule_notification — no URL configured → no-op
# ---------------------------------------------------------------------------


def test_schedule_notification_no_url(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "")
    from src.services.notification_client import schedule_notification

    # Should not raise, should not create any task
    schedule_notification({"category": "mention"})


def test_schedule_notifications_bulk_empty_list(monkeypatch):
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")
    from src.services.notification_client import schedule_notifications_bulk

    # Empty list → no-op
    schedule_notifications_bulk([])


# ---------------------------------------------------------------------------
# schedule_notification — with URL, within a running event loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_notification_creates_task(monkeypatch):
    """When NOTIFICATION_SERVICE_URL is set, a background task is created."""
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")
    monkeypatch.setenv("NOTIFICATION_SERVICE_TOKEN", "tok")

    posted_urls: list = []
    posted_payloads: list = []

    async def _fake_post(url: str, payload) -> None:
        posted_urls.append(url)
        posted_payloads.append(payload)

    import src.services.notification_client as nc
    with patch.object(nc, "_post", side_effect=_fake_post):
        nc.schedule_notification({"category": "mention", "user_id": "u1"})
        # Let background tasks run
        await asyncio.sleep(0)

    assert len(posted_urls) == 1
    assert "/internal/notifications" in posted_urls[0]
    assert posted_payloads[0]["category"] == "mention"


@pytest.mark.asyncio
async def test_schedule_notifications_bulk_single_uses_single_endpoint(monkeypatch):
    """A bulk call with one payload routes to the single endpoint."""
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    posted_urls: list = []

    async def _fake_post(url: str, payload) -> None:
        posted_urls.append(url)

    import src.services.notification_client as nc
    with patch.object(nc, "_post", side_effect=_fake_post):
        nc.schedule_notifications_bulk([{"category": "mention", "user_id": "u1"}])
        await asyncio.sleep(0)

    assert len(posted_urls) == 1
    assert "/internal/notifications" in posted_urls[0]
    assert "bulk" not in posted_urls[0]


@pytest.mark.asyncio
async def test_schedule_notifications_bulk_multiple_uses_bulk_endpoint(monkeypatch):
    """A bulk call with multiple payloads routes to the /bulk endpoint."""
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    posted_urls: list = []
    posted_payloads: list = []

    async def _fake_post(url: str, payload) -> None:
        posted_urls.append(url)
        posted_payloads.append(payload)

    import src.services.notification_client as nc
    with patch.object(nc, "_post", side_effect=_fake_post):
        nc.schedule_notifications_bulk([
            {"category": "mention", "user_id": "u1"},
            {"category": "mention", "user_id": "u2"},
        ])
        await asyncio.sleep(0)

    assert len(posted_urls) == 1
    assert "bulk" in posted_urls[0]
    assert len(posted_payloads[0]) == 2


# ---------------------------------------------------------------------------
# _post — HTTP and network error handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_swallows_http_error(monkeypatch):
    """_post does not raise when notification-service returns a non-2xx status."""
    monkeypatch.setenv("NOTIFICATION_SERVICE_URL", "http://notif:8080")

    mock_resp = MagicMock()
    mock_resp.status = 500
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.post = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    from src.services.notification_client import _post

    with patch("aiohttp.ClientSession", return_value=mock_session):
        # Must not raise
        await _post("http://notif:8080/internal/notifications", {"x": 1})


@pytest.mark.asyncio
async def test_post_swallows_network_exception(monkeypatch):
    """_post does not raise on network error."""
    from src.services.notification_client import _post

    with patch("aiohttp.ClientSession", side_effect=OSError("connection refused")):
        await _post("http://notif:8080/internal/notifications", {"x": 1})
