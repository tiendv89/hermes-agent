"""Integration tests for POST /api/v1/coding/chat — SSE endpoint.

Covers:
  - SSE stream with chat.completion.chunk + [DONE] sentinel.
  - hermes.tool.deferred events when the agent uses a coding (deferred) tool.
  - check_quota is called before every turn.
  - 401 when the shared GATEWAY_SERVICE_TOKEN is missing/wrong (workflow-bff
    verifies the IDE's device-flow JWT itself and forwards trusted identity
    headers — see src/api/coding_identity.py).
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Stub missing third-party modules
# ---------------------------------------------------------------------------


def _stub_mod(name: str) -> types.ModuleType:
    if name not in sys.modules:
        sys.modules[name] = types.ModuleType(name)
    return sys.modules[name]


_mcp = _stub_mod("mcp")
_mcp.ClientSession = MagicMock  # type: ignore[attr-defined]
_mcp_client = _stub_mod("mcp.client")
_mcp_client_sse = _stub_mod("mcp.client.sse")
_mcp_client_sse.sse_client = MagicMock  # type: ignore[attr-defined]
_mcp.client = _mcp_client  # type: ignore[attr-defined]

_hermes_cli = _stub_mod("hermes_cli")
_hermes_cli_plugins = _stub_mod("hermes_cli.plugins")
_hermes_cli_plugins.PluginContext = MagicMock  # type: ignore[attr-defined]
_hermes_cli_plugins.PluginManifest = MagicMock  # type: ignore[attr-defined]
_hermes_cli_plugins.get_plugin_manager = MagicMock  # type: ignore[attr-defined]
_hermes_cli.plugins = _hermes_cli_plugins  # type: ignore[attr-defined]

_hermes_state = _stub_mod("hermes_state")
_hermes_state.SessionDB = MagicMock  # type: ignore[attr-defined]

_run_agent = _stub_mod("run_agent")
_run_agent.AIAgent = MagicMock  # type: ignore[attr-defined]

_tools = _stub_mod("tools")
_tools.clarify_gateway = MagicMock  # type: ignore[attr-defined]

_hc = _stub_mod("hermes_constants")
_hc.get_hermes_home = MagicMock(return_value=REPO_ROOT)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Stub AIAgent
# ---------------------------------------------------------------------------


class _StubCodingAgent:
    """Stub AIAgent that emits one text delta, then optionally a tool call."""

    def __init__(self, **kwargs):
        self._delta_cb = kwargs.get("stream_delta_callback")
        self._tool_start_cb = kwargs.get("tool_start_callback")
        self._tool_complete_cb = kwargs.get("tool_complete_callback")
        self._reasoning_cb = kwargs.get("reasoning_callback")
        self.session_input_tokens = 100
        self.session_output_tokens = 50
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0

    def run_conversation(
        self, message, conversation_history=None, persist_user_message=None
    ):
        if self._delta_cb:
            self._delta_cb("Hello from coding agent")
        if getattr(self, "_emit_deferred", False):
            name = self._deferred_tool_name
            params = self._deferred_tool_params
            call_id = "call_abc"
            if self._tool_start_cb:
                self._tool_start_cb(call_id, name, params)
            if self._tool_complete_cb:
                self._tool_complete_cb(
                    call_id,
                    name,
                    params,
                    json.dumps(
                        {"__deferred__": True, "tool": name, "params": params}
                    ),
                )


class _FailingCodingAgent:
    """Stub AIAgent mirroring a real run_conversation() failure — it returns
    {"failed": True, "error": ...} without ever calling stream_delta_callback
    (matches agent.conversation_loop.run_conversation's real behavior on an
    exhausted-retries LLM call: invalid model, provider outage, rate limit,
    quota exhaustion, etc. all surface this way, not as a raised exception)."""

    def __init__(self, **kwargs):
        self.session_input_tokens = 0
        self.session_output_tokens = 0
        self.session_cache_read_tokens = 0
        self.session_cache_write_tokens = 0

    def run_conversation(
        self, message, conversation_history=None, persist_user_message=None
    ):
        return {
            "final_response": "API call failed after 3 retries: HTTP 404: Error code: 404",
            "completed": False,
            "failed": True,
            "error": "HTTP 404: Error code: 404",
        }


def _parse_sse_events(body: bytes) -> list:
    events = []
    current_event = None
    for line in body.decode().splitlines():
        if line.startswith("event: "):
            current_event = line[7:].strip()
            continue
        if not line.startswith("data: "):
            continue
        payload = line[6:].strip()
        if payload == "[DONE]":
            events.append({"type": "[DONE]"})
        else:
            try:
                parsed = json.loads(payload)
                if current_event:
                    parsed["_sse_event"] = current_event
                events.append(parsed)
            except json.JSONDecodeError:
                pass
            current_event = None
    return events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _cleanup_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_SERVICE_TOKEN", "test-service-token")
    monkeypatch.setenv("CODING_AGENT_MODEL", "test-model")
    yield


@pytest.fixture
def coding_app():
    """Minimal FastAPI app with the coding profile router.

    /coding/chat and /coding/models both depend on get_db (model-catalog
    lookups) — overridden here with a stub since none of these tests send an
    explicit body.model (empty string short-circuits _resolve_model before
    it ever touches the session), so the stub is never actually queried.
    """
    sys.modules["run_agent"].AIAgent = _StubCodingAgent  # type: ignore[attr-defined]

    from fastapi import FastAPI

    from profiles.coding.setup import build_router
    from src.api.deps import get_db

    async def _fake_get_db():
        yield MagicMock()

    app = FastAPI()
    app.include_router(build_router(), prefix="/api/v1")
    app.dependency_overrides[get_db] = _fake_get_db
    return app


@pytest.fixture
def auth_headers():
    """Trusted headers workflow-bff injects after verifying the IDE's JWT itself."""
    return {
        "Authorization": "Bearer test-service-token",
        "X-User-Id": "test-user",
        "X-Org-Id": "test-org",
        "X-Accessible-Org-Ids": "test-org",
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_coding_chat_returns_sse_events(coding_app, auth_headers):
    """POST /api/v1/coding/chat → SSE stream with content + [DONE]."""
    with patch(
        "src.api.routers.coding_chat.check_quota",
        AsyncMock(return_value=MagicMock(allowed=True)),
    ), patch(
        "src.api.routers.coding_chat.emit_turn_cost",
        AsyncMock(),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=coding_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v1/coding/chat",
                json={
                    "messages": [{"role": "user", "content": "Hello"}],
                    "workspace_id": "ws-1",
                    "repo_path": "/tmp/test",
                    "context": {
                        "active_file": "src/main.py",
                        "workspace_root": "/tmp/test",
                    },
                },
                headers=auth_headers,
                timeout=15.0,
            )

    assert resp.status_code == 200, (
        f"Unexpected status: {resp.status_code} — {resp.text}"
    )
    assert "text/event-stream" in resp.headers.get("content-type", "")

    events = _parse_sse_events(resp.content)

    def _content(e: dict) -> str | None:
        if e.get("object") != "chat.completion.chunk":
            return None
        choices = e.get("choices") or [{}]
        return choices[0].get("delta", {}).get("content")

    content_events = [e for e in events if _content(e)]
    done_events = [e for e in events if e.get("type") == "[DONE]"]

    assert len(content_events) >= 1, f"Expected ≥1 content chunk, got: {events}"
    assert len(done_events) >= 1, f"Expected [DONE], got: {events}"
    full_content = "".join(_content(e) or "" for e in content_events)
    assert full_content == "Hello from coding agent"


@pytest.mark.asyncio
async def test_coding_chat_emits_deferred_tool_event(coding_app, auth_headers):
    """When the agent calls a deferred tool, a hermes.tool.deferred event is emitted."""

    class _DeferredAgent(_StubCodingAgent):
        _emit_deferred = True
        _deferred_tool_name = "edit_file"
        _deferred_tool_params = {
            "path": "src/main.py",
            "edits": [{"old_string": "x = 1", "new_string": "x = 2"}],
        }

    sys.modules["run_agent"].AIAgent = _DeferredAgent  # type: ignore[attr-defined]

    with patch(
        "src.api.routers.coding_chat.check_quota",
        AsyncMock(return_value=MagicMock(allowed=True)),
    ), patch(
        "src.api.routers.coding_chat.emit_turn_cost",
        AsyncMock(),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=coding_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v1/coding/chat",
                json={
                    "messages": [{"role": "user", "content": "Edit main.py"}],
                    "workspace_id": "ws-1",
                    "repo_path": "/tmp/test",
                    "context": {"workspace_root": "/tmp/test"},
                },
                headers=auth_headers,
                timeout=15.0,
            )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.content)

    deferred_events = [
        e for e in events if e.get("_sse_event") == "hermes.tool.deferred"
    ]
    assert len(deferred_events) >= 1, (
        f"Expected ≥1 hermes.tool.deferred event, got events: {events}"
    )
    deferred = deferred_events[0]
    assert deferred["tool"] == "edit_file"
    assert deferred["params"]["path"] == "src/main.py"
    assert deferred["params"]["edits"][0]["old_string"] == "x = 1"


@pytest.mark.asyncio
async def test_coding_chat_calls_check_quota(coding_app, auth_headers):
    """The coding endpoint calls check_quota before the agent turn."""
    mock_check_quota = AsyncMock(return_value=MagicMock(allowed=True))

    with patch(
        "src.api.routers.coding_chat.check_quota", mock_check_quota
    ), patch(
        "src.api.routers.coding_chat.emit_turn_cost", AsyncMock()
    ):
        async with AsyncClient(
            transport=ASGITransport(app=coding_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v1/coding/chat",
                json={
                    "messages": [{"role": "user", "content": "Hello"}],
                    "workspace_id": "ws-1",
                    "repo_path": "/tmp/test",
                    "context": {"workspace_root": "/tmp/test"},
                },
                headers=auth_headers,
                timeout=15.0,
            )

    assert resp.status_code == 200
    assert mock_check_quota.called, "check_quota was not called"
    call_args = mock_check_quota.call_args
    assert call_args[1].get("org_id") == "test-org"


@pytest.mark.asyncio
async def test_coding_chat_quota_blocked(coding_app, auth_headers):
    """When check_quota returns allowed=False, the endpoint streams a block message."""
    mock_check_quota = AsyncMock(
        return_value=MagicMock(
            allowed=False,
            reason="daily_exceeded",
            resets_at="tomorrow",
            daily_cap="1000 credits",
        )
    )

    with patch(
        "src.api.routers.coding_chat.check_quota", mock_check_quota
    ), patch(
        "src.api.routers.coding_chat.emit_turn_cost", AsyncMock()
    ):
        async with AsyncClient(
            transport=ASGITransport(app=coding_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v1/coding/chat",
                json={
                    "messages": [{"role": "user", "content": "Hello"}],
                    "workspace_id": "ws-1",
                    "repo_path": "/tmp/test",
                    "context": {"workspace_root": "/tmp/test"},
                },
                headers=auth_headers,
                timeout=15.0,
            )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.content)

    def _content(e: dict) -> str | None:
        if e.get("object") != "chat.completion.chunk":
            return None
        choices = e.get("choices") or [{}]
        return choices[0].get("delta", {}).get("content")

    full_text = "".join(_content(e) or "" for e in events if _content(e))
    assert "daily credit limit" in full_text.lower()


@pytest.mark.asyncio
async def test_coding_chat_agent_failure_surfaces_error(coding_app, auth_headers):
    """A run_conversation() failure (e.g. exhausted-retries LLM call) must
    surface as an error frame, not silently complete as an empty success —
    the real hermes-agent run_conversation() returns {"failed": True, ...}
    rather than raising, so _run_coding_agent_turn must check for it."""
    sys.modules["run_agent"].AIAgent = _FailingCodingAgent  # type: ignore[attr-defined]

    with patch(
        "src.api.routers.coding_chat.check_quota",
        AsyncMock(return_value=MagicMock(allowed=True)),
    ), patch(
        "src.api.routers.coding_chat.emit_turn_cost",
        AsyncMock(),
    ) as mock_emit_cost:
        async with AsyncClient(
            transport=ASGITransport(app=coding_app),
            base_url="http://testserver",
        ) as client:
            resp = await client.post(
                "/api/v1/coding/chat",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "workspace_id": "ws-1",
                    "repo_path": "/tmp/test",
                    "context": {"workspace_root": "/tmp/test"},
                },
                headers=auth_headers,
                timeout=15.0,
            )

    assert resp.status_code == 200
    events = _parse_sse_events(resp.content)

    finish_reasons = [
        e.get("choices", [{}])[0].get("finish_reason")
        for e in events
        if e.get("object") == "chat.completion.chunk"
    ]
    assert "error" in finish_reasons, f"Expected an error finish_reason, got: {events}"

    error_frame = next(
        e
        for e in events
        if e.get("object") == "chat.completion.chunk"
        and e.get("choices", [{}])[0].get("finish_reason") == "error"
    )
    assert "404" in error_frame["hermes"]["error"]

    # A failed turn never reaches the post-turn cost emission.
    mock_emit_cost.assert_not_called()


@pytest.mark.asyncio
async def test_coding_chat_requires_auth(coding_app):
    """POST without the shared service token returns 401.

    workflow-bff verifies the IDE's device-flow JWT itself and only forwards
    requests behind GATEWAY_SERVICE_TOKEN — this service never sees the JWT.
    """
    async with AsyncClient(
        transport=ASGITransport(app=coding_app),
        base_url="http://testserver",
    ) as client:
        resp = await client.post(
            "/api/v1/coding/chat",
            json={
                "messages": [{"role": "user", "content": "Hello"}],
                "workspace_id": "ws-1",
                "repo_path": "/tmp/test",
                "context": {"workspace_root": "/tmp/test"},
            },
            timeout=15.0,
        )

    assert resp.status_code == 401, (
        f"Expected 401, got {resp.status_code}: {resp.text}"
    )
