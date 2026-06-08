"""Integration test: POST /api/v5/stream_chat returns SSE events.

Covers the second half of the T1 smoke-test subtask from tasks.md:
    "stream_chat returns SSE for a simple query"

Uses a minimal FastAPI test app (no Postgres lifespan) with mocked session
functions and a stub AIAgent. Verifies that:
  - the endpoint returns HTTP 200 with content-type text/event-stream
  - at least one OpenAI chat.completion.chunk carrying delta.content is emitted
  - the stream terminates with a [DONE] sentinel
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class _MockAIAgent:
    """Minimal AIAgent stub — emits one text delta then returns."""

    def __init__(self, stream_delta_callback=None, **kwargs):
        self._delta_cb = stream_delta_callback

    def run_conversation(self, message, conversation_history=None):
        if self._delta_cb:
            self._delta_cb("Hello from mock agent")


def _inject_mock_run_agent():
    """Inject a fake run_agent module into sys.modules before router import.

    The router does `from run_agent import AIAgent` inside _run_agent().
    run_agent has heavyweight deps that aren't installed in the test env, so
    we pre-populate sys.modules with a minimal stub module instead.
    """
    if "run_agent" not in sys.modules:
        stub = types.ModuleType("run_agent")
        stub.AIAgent = _MockAIAgent  # type: ignore[attr-defined]
        sys.modules["run_agent"] = stub


def _parse_sse_events(body: bytes) -> list:
    """Parse SSE data lines from a response body into a list of dicts."""
    events = []
    for line in body.decode().splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            events.append({"type": "[DONE]"})
        else:
            try:
                events.append(json.loads(payload))
            except json.JSONDecodeError:
                pass
    return events


@pytest.fixture
def stream_chat_app():
    """Minimal FastAPI app with the workflow_gateway router and a dummy db_session."""
    _inject_mock_run_agent()

    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from workflow_gateway.api.router import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v5")

    # db_session must be an async context manager factory (async with db_session() as s)
    @asynccontextmanager
    async def _db_session():
        yield MagicMock()

    app.state.db_session = _db_session
    return app


@pytest.mark.asyncio
async def test_stream_chat_returns_sse_events(stream_chat_app):
    """POST /api/v5/stream_chat → ≥1 message_output_partial event + [DONE] sentinel."""
    from httpx import ASGITransport, AsyncClient

    # Use MagicMock so attribute access (session.title) works correctly.
    session_mock = MagicMock()
    session_mock.title = "existing title"  # non-null → auto-title skipped

    with (
        patch(
            "workflow_gateway.api.router.get_session",
            AsyncMock(return_value=session_mock),
        ),
        patch(
            "workflow_gateway.api.router.get_messages_as_conversation",
            AsyncMock(return_value=[]),
        ),
        patch("workflow_gateway.api.router.set_session_title", AsyncMock()),
        patch("workflow_gateway.api.router.touch_session", AsyncMock()),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=stream_chat_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v5/stream_chat",
                json={
                    "session_id": "sess_test_t1",
                    "message": "Hello",
                    "workspace_id": "ws-1",
                    "feature_id": "feat-1",
                },
                timeout=15.0,
            )

    assert resp.status_code == 200, (
        f"Unexpected status: {resp.status_code} — {resp.text}"
    )
    assert "text/event-stream" in resp.headers.get("content-type", ""), (
        f"Expected text/event-stream, got: {resp.headers.get('content-type')}"
    )

    events = _parse_sse_events(resp.content)

    def _content(e: dict) -> str | None:
        if e.get("object") != "chat.completion.chunk":
            return None
        choices = e.get("choices") or [{}]
        return choices[0].get("delta", {}).get("content")

    content_events = [e for e in events if _content(e)]
    done_events = [e for e in events if e.get("type") == "[DONE]"]

    assert len(content_events) >= 1, (
        f"Expected ≥1 chat.completion.chunk with delta.content, got events: {events}"
    )
    assert len(done_events) >= 1, (
        f"Expected [DONE] sentinel in stream, got events: {events}"
    )
    assert _content(content_events[0]) == "Hello from mock agent"


@pytest.mark.asyncio
async def test_stream_chat_rejects_concurrent_run(stream_chat_app):
    """A second stream_chat for a session already running returns 409 — this is
    what stops the transcript from being double-persisted on reconnect."""
    from httpx import ASGITransport, AsyncClient
    from workflow_gateway.api import router as router_mod

    session_mock = MagicMock()
    session_mock.title = "existing title"

    # Pretend a run for this session is already in flight.
    router_mod._active_runs.add("sess_busy")
    try:
        with (
            patch(
                "workflow_gateway.api.router.get_session",
                AsyncMock(return_value=session_mock),
            ),
            patch(
                "workflow_gateway.api.router.get_messages_as_conversation",
                AsyncMock(return_value=[]),
            ),
            patch("workflow_gateway.api.router.set_session_title", AsyncMock()),
            patch("workflow_gateway.api.router.touch_session", AsyncMock()),
        ):
            async with AsyncClient(
                transport=ASGITransport(app=stream_chat_app),
                base_url="http://testserver",
            ) as client:
                resp = await client.post(
                    "/api/v5/stream_chat",
                    json={"session_id": "sess_busy", "message": "Hello"},
                    timeout=15.0,
                )
        assert resp.status_code == 409, f"Expected 409, got {resp.status_code}"
    finally:
        router_mod._active_runs.discard("sess_busy")
