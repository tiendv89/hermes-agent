"""Tests for GET /api/v1/notifications/stream — the global per-user SSE
subscription that powers real-time toasts on the frontend.
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.mark.asyncio
async def test_stream_requires_identity():
    """No X-User-Id → require_identity rejects before the route body runs."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.routers.members import router as members_router

    app = FastAPI()
    app.include_router(members_router, prefix="/api/v1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/notifications/stream")

    assert resp.status_code in (400, 401, 422)


@pytest.mark.asyncio
async def test_stream_delivers_published_event_to_caller():
    """An event published to the caller's user_id topic is delivered as an
    SSE frame on their /notifications/stream connection.

    Drives the endpoint's StreamingResponse.body_iterator directly instead of
    going through httpx/ASGITransport — the endpoint's loop has no natural
    termination (unlike the thread-scoped stream's channel.deleted signal),
    and consuming it through a real HTTP client hangs the whole test run.
    """
    from src.api.identity import Identity
    from src.api.routers.members import stream_notifications
    from src.realtime.user_bus import get_user_bus

    identity = Identity(user_id="user-1", org_id="org_1")
    resp = await stream_notifications(identity=identity)

    body_iter = resp.body_iterator

    get_user_bus().publish(
        "user-1",
        {"event": "notification.created", "data": {"id": "n1", "summary": "hi"}},
    )

    frame = await asyncio.wait_for(anext(body_iter), timeout=2.0)
    await body_iter.aclose()

    assert "event: notification.created" in frame
    assert '"id": "n1"' in frame


@pytest.mark.asyncio
async def test_stream_missing_user_id_returns_400():
    """require_identity returning an Identity with no user_id → 400 from the handler itself."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.api.identity import Identity, require_identity
    from src.api.routers.members import router as members_router

    app = FastAPI()
    app.include_router(members_router, prefix="/api/v1")
    app.dependency_overrides[require_identity] = lambda: Identity(user_id="", org_id="org_1")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/api/v1/notifications/stream")

    assert resp.status_code == 400
