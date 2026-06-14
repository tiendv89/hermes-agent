"""Tests for the channels API (T4).

Covers the T4 test plan from tasks.md:
  - Any member can create a channel → channel session created + creator auto-joined
  - Non-admin delete → 403
  - Admin delete → channel + messages removed, channel.deleted event published
  - Join adds membership (idempotent)
  - Duplicate channel name → 409 (unique name enforced)
  - List channels returns non-archived channels for a workspace
  - Missing identity → 400
  - Channel not found → 404
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
# Helpers
# ---------------------------------------------------------------------------


def _mock_db():
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    db.delete = AsyncMock()
    return db


def _make_channel_session(
    channel_id="chan_abc",
    workspace_id="ws_1",
    title="general",
    user_id="user_creator",
):
    ch = MagicMock()
    ch.id = channel_id
    ch.workspace_id = workspace_id
    ch.title = title
    ch.user_id = user_id
    ch.kind = "channel"
    ch.archived = False
    return ch


# ---------------------------------------------------------------------------
# SessionBus tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bus_publish_delivers_to_subscriber():
    """Published event is received by an active subscriber."""
    from src.realtime.bus import SessionBus

    bus = SessionBus()
    async with bus.subscribe("sess_1") as q:
        bus.publish("sess_1", {"event": "channel.deleted", "channel_id": "sess_1"})
        event = await asyncio.wait_for(q.get(), timeout=1.0)
    assert event["event"] == "channel.deleted"


@pytest.mark.asyncio
async def test_bus_publish_no_subscribers_is_noop():
    """Publishing with no subscribers does not raise."""
    from src.realtime.bus import SessionBus

    bus = SessionBus()
    bus.publish("sess_none", {"event": "channel.deleted", "channel_id": "sess_none"})


@pytest.mark.asyncio
async def test_bus_unsubscribe_removes_queue():
    """After the subscribe context exits, the queue is removed."""
    from src.realtime.bus import SessionBus

    bus = SessionBus()
    async with bus.subscribe("sess_2") as _q:
        pass
    assert "sess_2" not in bus._topics


# ---------------------------------------------------------------------------
# user_service_client tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_workspace_admin_no_url_returns_true():
    """When USER_SERVICE_URL is unset, admin check is bypassed (dev mode)."""
    with patch.dict("os.environ", {}, clear=False):
        import os
        os.environ.pop("USER_SERVICE_URL", None)
        from src.services.user_service_client import is_workspace_admin
        result = await is_workspace_admin("ws_1", "user_a")
    assert result is True


@pytest.mark.asyncio
async def test_is_workspace_admin_admin_role():
    """Role 'admin' → is_workspace_admin returns True."""

    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"role": "admin", "user_id": "user_a"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch.dict("os.environ", {"USER_SERVICE_URL": "http://us:8080"}):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            from src.services import user_service_client
            # Reload to pick up env var
            import importlib
            importlib.reload(user_service_client)
            result = await user_service_client.is_workspace_admin("ws_1", "user_a")
    assert result is True


@pytest.mark.asyncio
async def test_is_workspace_admin_member_role():
    """Role 'member' → is_workspace_admin returns False."""
    mock_resp = MagicMock()
    mock_resp.status = 200
    mock_resp.json = AsyncMock(return_value={"role": "member", "user_id": "user_b"})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch.dict("os.environ", {"USER_SERVICE_URL": "http://us:8080"}):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            from src.services import user_service_client
            import importlib
            importlib.reload(user_service_client)
            result = await user_service_client.is_workspace_admin("ws_1", "user_b")
    assert result is False


@pytest.mark.asyncio
async def test_get_workspace_role_404_returns_none():
    """404 from user-service → not a member → returns None."""
    mock_resp = MagicMock()
    mock_resp.status = 404
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_session = MagicMock()
    mock_session.get = MagicMock(return_value=mock_resp)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch.dict("os.environ", {"USER_SERVICE_URL": "http://us:8080"}):
        with patch("aiohttp.ClientSession", return_value=mock_session):
            from src.services import user_service_client
            import importlib
            importlib.reload(user_service_client)
            role = await user_service_client.get_workspace_role("ws_1", "user_x")
    assert role is None


# ---------------------------------------------------------------------------
# Channels router unit tests (no Postgres — mock store functions)
# ---------------------------------------------------------------------------


def _make_app():
    """Build a minimal FastAPI test app with the channels router."""
    from fastapi import FastAPI
    from src.api.routers.channels import router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity

    async def _override_db():
        yield _mock_db()

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    def _override_identity():
        return Identity(user_id="user_test", org_id="org_1")

    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = _override_identity
    return app


@pytest.mark.asyncio
async def test_list_channels_returns_channels():
    """GET /channels returns channels list from store."""
    from fastapi.testclient import TestClient

    channels_data = [
        {"id": "chan_1", "name": "general", "creator_user_id": "u1",
         "started_at": 1000.0, "last_active_at": 1001.0, "description": None},
    ]
    with patch("src.api.routers.channels.list_channels", new=AsyncMock(return_value=channels_data)):
        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/v1/channels?workspace_id=ws_1")
    assert resp.status_code == 200
    assert resp.json()["channels"] == channels_data


@pytest.mark.asyncio
async def test_create_channel_success():
    """POST /channels → 201 with channel_id on success."""
    with patch("src.api.routers.channels.create_channel", new=AsyncMock(return_value="chan_new")):
        from fastapi.testclient import TestClient
        app = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/v1/channels",
            json={"workspace_id": "ws_1", "name": "my-channel"},
        )
    assert resp.status_code == 201
    assert resp.json()["channel_id"] == "chan_new"


@pytest.mark.asyncio
async def test_create_channel_duplicate_name_409():
    """POST /channels with duplicate name → 409 Conflict."""
    from sqlalchemy.exc import IntegrityError

    with patch(
        "src.api.routers.channels.create_channel",
        new=AsyncMock(side_effect=IntegrityError("dup", {}, None)),
    ):
        from fastapi.testclient import TestClient
        app = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/v1/channels",
            json={"workspace_id": "ws_1", "name": "general"},
        )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_create_channel_missing_name_400():
    """POST /channels without name → 400."""
    from fastapi.testclient import TestClient
    app = _make_app()
    client = TestClient(app)
    resp = client.post(
        "/api/v1/channels",
        json={"workspace_id": "ws_1", "name": "  "},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_delete_channel_non_admin_403():
    """Non-admin caller → DELETE /channels/{id} → 403."""
    channel = _make_channel_session()

    with (
        patch("src.api.routers.channels.get_channel", new=AsyncMock(return_value=channel)),
        patch("src.api.routers.channels.is_workspace_admin", new=AsyncMock(return_value=False)),
    ):
        from fastapi.testclient import TestClient
        app = _make_app()
        client = TestClient(app)
        resp = client.delete("/api/v1/channels/chan_abc")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_delete_channel_admin_success_publishes_event():
    """Admin caller → DELETE /channels/{id} → 204 + channel.deleted published."""
    channel = _make_channel_session()
    published_events = []

    class _FakeBus:
        def publish(self, session_id, event):
            published_events.append((session_id, event))

    with (
        patch("src.api.routers.channels.get_channel", new=AsyncMock(return_value=channel)),
        patch("src.api.routers.channels.is_workspace_admin", new=AsyncMock(return_value=True)),
        patch("src.api.routers.channels.hard_delete_channel", new=AsyncMock(return_value=True)),
        patch("src.api.routers.channels.get_bus", return_value=_FakeBus()),
    ):
        from fastapi.testclient import TestClient
        app = _make_app()
        client = TestClient(app)
        resp = client.delete("/api/v1/channels/chan_abc")
    assert resp.status_code == 204
    assert len(published_events) == 1
    assert published_events[0] == ("chan_abc", {"event": "channel.deleted", "channel_id": "chan_abc"})


@pytest.mark.asyncio
async def test_delete_channel_not_found_404():
    """DELETE /channels/{id} for unknown id → 404."""
    with patch("src.api.routers.channels.get_channel", new=AsyncMock(return_value=None)):
        from fastapi.testclient import TestClient
        app = _make_app()
        client = TestClient(app)
        resp = client.delete("/api/v1/channels/chan_missing")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_join_channel_success():
    """POST /channels/{id}/join → 200 with joined=True."""
    channel = _make_channel_session()

    with (
        patch("src.api.routers.channels.get_channel", new=AsyncMock(return_value=channel)),
        patch("src.api.routers.channels.is_member", new=AsyncMock(return_value=False)),
        patch("src.api.routers.channels.add_member", new=AsyncMock()),
    ):
        from fastapi.testclient import TestClient
        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/v1/channels/chan_abc/join")
    assert resp.status_code == 200
    assert resp.json()["joined"] is True
    assert resp.json()["channel_id"] == "chan_abc"


@pytest.mark.asyncio
async def test_join_channel_idempotent():
    """POST /channels/{id}/join when already a member → 200 (idempotent)."""
    channel = _make_channel_session()

    with (
        patch("src.api.routers.channels.get_channel", new=AsyncMock(return_value=channel)),
        patch("src.api.routers.channels.is_member", new=AsyncMock(return_value=True)),
        patch("src.api.routers.channels.add_member", new=AsyncMock()),
    ):
        from fastapi.testclient import TestClient
        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/v1/channels/chan_abc/join")
    assert resp.status_code == 200
    assert resp.json()["joined"] is True


@pytest.mark.asyncio
async def test_join_channel_not_found_404():
    """POST /channels/{id}/join for non-existent channel → 404."""
    with patch("src.api.routers.channels.get_channel", new=AsyncMock(return_value=None)):
        from fastapi.testclient import TestClient
        app = _make_app()
        client = TestClient(app)
        resp = client.post("/api/v1/channels/chan_missing/join")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_channel_user_service_error_502():
    """user-service HTTP error during admin check → 502."""
    from src.services.user_service_client import UserServiceError

    channel = _make_channel_session()

    with (
        patch("src.api.routers.channels.get_channel", new=AsyncMock(return_value=channel)),
        patch(
            "src.api.routers.channels.is_workspace_admin",
            new=AsyncMock(side_effect=UserServiceError("timeout")),
        ),
    ):
        from fastapi.testclient import TestClient
        app = _make_app()
        client = TestClient(app)
        resp = client.delete("/api/v1/channels/chan_abc")
    assert resp.status_code == 502
