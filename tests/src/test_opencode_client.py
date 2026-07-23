"""Tests for the opencode server client (HTTP only).

Covers:
  - check_opencode_available: true/false on OPENCODE_SERVER_URL presence
  - _resolve_config / requests: missing config raises missing_config
  - create_session: payload construction, id extraction, missing-id error
  - register_mcp_bridge: success, non-"connected" status raises
  - send_message: nested model{providerID,modelID} payload shape, optional
    agent/tools/system fields, blocking timeout param passthrough
  - extract_text / extract_reasoning / extract_usage / extract_error: shape
    parsing against opencode's confirmed Part/AssistantMessage schemas
  - Basic auth header included only when OPENCODE_SERVER_PASSWORD is set
  - 4xx/5xx surfaces status on the raised error
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio  # noqa: F401 — registers the asyncio backend

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# Unlike test_vcs_service_client.py, no test here exercises run_async() (the
# only opencode_client function that touches plugins.context), so there is
# deliberately no plugins.context stub/real-module setup in this file.
# Earlier this file carried the same defensive `_ensure_plugins_context()`
# boilerplate as test_vcs_service_client.py, copy-pasted without checking
# whether it was actually needed — it forced the REAL plugins.context module
# into sys.modules and left it cached there. Because this file's name sorts
# alphabetically before test_quota_cost.py/test_stream_chat.py, that real
# module then leaked into THEIR test runs, which each expect
# sys.modules["plugins.context"] to still be an empty/stub module so their
# own _inject_stubs()-style helpers can populate it with MagicMocks — with
# the real module already cached, their `hasattr(ctx, "set_context")` check
# saw a real function and skipped the mock, breaking assertions. Reproduced
# and confirmed via `git stash` bisection while adding this file to the
# suite; removing the unused block fixed it.


def _import_client():
    import src.services.opencode_client as mod

    return mod


class _FakeResponse:
    def __init__(self, status: int, body):
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.request_calls = []

    def request(self, method, url, *, json=None, auth=None, timeout=None):
        self.request_calls.append(
            {"method": method, "url": url, "json": json, "auth": auth, "timeout": timeout}
        )
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# check_opencode_available
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    def test_false_when_url_missing(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_SERVER_URL", raising=False)
        mod = _import_client()
        assert mod.check_opencode_available() is False

    def test_true_when_url_set(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        assert mod.check_opencode_available() is True


# ---------------------------------------------------------------------------
# Missing config
# ---------------------------------------------------------------------------


class TestMissingConfig:
    @pytest.mark.asyncio
    async def test_create_session_missing_url_raises(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_SERVER_URL", raising=False)
        mod = _import_client()
        with pytest.raises(mod.OpencodeClientError) as exc_info:
            await mod.create_session()
        assert exc_info.value.reason_code == "missing_config"

    @pytest.mark.asyncio
    async def test_send_message_missing_url_raises(self, monkeypatch):
        monkeypatch.delenv("OPENCODE_SERVER_URL", raising=False)
        mod = _import_client()
        with pytest.raises(mod.OpencodeClientError) as exc_info:
            await mod.send_message(
                "ses_1", "hi", provider_id="anthropic", model_id="claude-sonnet-4-6"
            )
        assert exc_info.value.reason_code == "missing_config"


# ---------------------------------------------------------------------------
# create_session
# ---------------------------------------------------------------------------


class TestCreateSession:
    @pytest.mark.asyncio
    async def test_returns_id(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"id": "ses_abc123"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            session_id = await mod.create_session()
        assert session_id == "ses_abc123"

    @pytest.mark.asyncio
    async def test_missing_id_in_response_raises(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            with pytest.raises(mod.OpencodeClientError):
                await mod.create_session()

    @pytest.mark.asyncio
    async def test_optional_title_and_agent_included_when_set(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"id": "ses_1"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.create_session(title="my turn", agent="build")
        payload = fake_session.request_calls[0]["json"]
        assert payload["title"] == "my turn"
        assert payload["agent"] == "build"

    @pytest.mark.asyncio
    async def test_title_and_agent_omitted_when_empty(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"id": "ses_1"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.create_session()
        payload = fake_session.request_calls[0]["json"]
        assert "title" not in payload
        assert "agent" not in payload


# ---------------------------------------------------------------------------
# register_mcp_bridge
# ---------------------------------------------------------------------------


class TestRegisterMcpBridge:
    @pytest.mark.asyncio
    async def test_connected_status_succeeds(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(
            _FakeResponse(200, {"ide-bridge": {"status": "connected"}})
        )
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.register_mcp_bridge("ide-bridge", "http://gw/mcp-bridge/ses_1/mcp")

        payload = fake_session.request_calls[0]["json"]
        assert payload["name"] == "ide-bridge"
        assert payload["config"]["type"] == "remote"
        assert payload["config"]["url"] == "http://gw/mcp-bridge/ses_1/mcp"

    @pytest.mark.asyncio
    async def test_failed_status_raises(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(
            _FakeResponse(
                200, {"ide-bridge": {"status": "failed", "error": "connection refused"}}
            )
        )
        with patch("aiohttp.ClientSession", return_value=fake_session):
            with pytest.raises(mod.OpencodeClientError):
                await mod.register_mcp_bridge("ide-bridge", "http://gw/mcp-bridge/ses_1/mcp")


# ---------------------------------------------------------------------------
# send_message — payload shape
# ---------------------------------------------------------------------------


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_model_payload_uses_nested_provider_and_model_id(self, monkeypatch):
        """opencode's confirmed schema is model: {providerID, modelID} — a
        nested object, not flat top-level fields. Sending the flat shape is
        silently accepted but ignored by the server (it falls back to
        whatever default model is configured), which is exactly the kind
        of quiet failure this test guards against."""
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"info": {}, "parts": []}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.send_message(
                "ses_1", "implement task 3", provider_id="anthropic", model_id="claude-sonnet-4-6"
            )
        payload = fake_session.request_calls[0]["json"]
        assert payload["model"] == {"providerID": "anthropic", "modelID": "claude-sonnet-4-6"}
        assert "providerID" not in payload
        assert "modelID" not in payload
        assert payload["parts"] == [{"type": "text", "text": "implement task 3"}]

    @pytest.mark.asyncio
    async def test_optional_fields_included_when_set(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"info": {}, "parts": []}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.send_message(
                "ses_1",
                "hi",
                provider_id="anthropic",
                model_id="claude-sonnet-4-6",
                agent="coding-bridge",
                tools={"bash": False, "read": False},
                system="You are a coding assistant.",
                variant="high",
            )
        payload = fake_session.request_calls[0]["json"]
        assert payload["agent"] == "coding-bridge"
        assert payload["tools"] == {"bash": False, "read": False}
        assert payload["system"] == "You are a coding assistant."
        assert payload["variant"] == "high"

    @pytest.mark.asyncio
    async def test_optional_fields_omitted_when_unset(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"info": {}, "parts": []}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.send_message(
                "ses_1", "hi", provider_id="anthropic", model_id="claude-sonnet-4-6"
            )
        payload = fake_session.request_calls[0]["json"]
        assert "agent" not in payload
        assert "tools" not in payload
        assert "system" not in payload
        assert "variant" not in payload

    @pytest.mark.asyncio
    async def test_url_targets_correct_session(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"info": {}, "parts": []}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.send_message(
                "ses_xyz", "hi", provider_id="anthropic", model_id="claude-sonnet-4-6"
            )
        assert fake_session.request_calls[0]["url"] == "http://localhost:4096/session/ses_xyz/message"

    @pytest.mark.asyncio
    async def test_custom_timeout_passed_through(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"info": {}, "parts": []}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.send_message(
                "ses_1", "hi", provider_id="anthropic", model_id="claude-sonnet-4-6", timeout=600
            )
        assert fake_session.request_calls[0]["timeout"].total == 600


# ---------------------------------------------------------------------------
# Basic auth
# ---------------------------------------------------------------------------


class TestBasicAuth:
    @pytest.mark.asyncio
    async def test_no_auth_when_password_unset(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"id": "ses_1"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.create_session()
        assert fake_session.request_calls[0]["auth"] is None

    @pytest.mark.asyncio
    async def test_basic_auth_when_password_set(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
        monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "secret")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"id": "ses_1"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.create_session()
        auth = fake_session.request_calls[0]["auth"]
        assert auth is not None
        assert auth.login == ""
        assert auth.password == "secret"

    @pytest.mark.asyncio
    async def test_basic_auth_uses_configured_username(self, monkeypatch):
        """Regression test: opencode rejects an empty Basic-Auth username once
        a password is set (confirmed live) — OPENCODE_SERVER_USERNAME must be
        sent, not silently dropped."""
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "admin")
        monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "secret")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(200, {"id": "ses_1"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.create_session()
        auth = fake_session.request_calls[0]["auth"]
        assert auth is not None
        assert auth.login == "admin"
        assert auth.password == "secret"


# ---------------------------------------------------------------------------
# HTTP errors
# ---------------------------------------------------------------------------


class TestHttpErrors:
    @pytest.mark.asyncio
    async def test_4xx_surfaces_status(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(400, {"error": "bad request"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            with pytest.raises(mod.OpencodeClientError) as exc_info:
                await mod.create_session()
        assert exc_info.value.status == 400

    @pytest.mark.asyncio
    async def test_5xx_surfaces_status(self, monkeypatch):
        monkeypatch.setenv("OPENCODE_SERVER_URL", "http://localhost:4096")
        mod = _import_client()
        fake_session = _FakeSession(_FakeResponse(500, {"error": "internal"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            with pytest.raises(mod.OpencodeClientError) as exc_info:
                await mod.create_session()
        assert exc_info.value.status == 500


# ---------------------------------------------------------------------------
# extract_text / extract_reasoning / extract_usage / extract_error
# ---------------------------------------------------------------------------


class TestExtractors:
    def test_extract_text_concatenates_text_parts_only(self):
        mod = _import_client()
        response = {
            "parts": [
                {"type": "text", "text": "Hello "},
                {"type": "reasoning", "text": "thinking..."},
                {"type": "text", "text": "world"},
                {"type": "tool", "tool": "read_file"},
            ]
        }
        assert mod.extract_text(response) == "Hello world"

    def test_extract_text_empty_parts(self):
        mod = _import_client()
        assert mod.extract_text({"parts": []}) == ""
        assert mod.extract_text({}) == ""

    def test_extract_reasoning_concatenates_reasoning_parts_only(self):
        mod = _import_client()
        response = {
            "parts": [
                {"type": "reasoning", "text": "step 1. "},
                {"type": "text", "text": "answer"},
                {"type": "reasoning", "text": "step 2."},
            ]
        }
        assert mod.extract_reasoning(response) == "step 1. step 2."

    def test_extract_usage_shape(self):
        mod = _import_client()
        response = {
            "info": {
                "cost": 0.0123,
                "tokens": {
                    "input": 100,
                    "output": 50,
                    "reasoning": 10,
                    "cache": {"read": 5, "write": 2},
                },
            }
        }
        usage = mod.extract_usage(response)
        assert usage == {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 5,
            "cache_write_tokens": 2,
        }

    def test_extract_usage_missing_fields_default_to_zero(self):
        mod = _import_client()
        assert mod.extract_usage({}) == {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
        }

    def test_extract_error_none_when_absent(self):
        mod = _import_client()
        assert mod.extract_error({"info": {}}) is None
        assert mod.extract_error({}) is None

    def test_extract_error_dict_shape(self):
        mod = _import_client()
        response = {
            "info": {
                "error": {
                    "name": "APIError",
                    "data": {"message": "No provider available"},
                }
            }
        }
        assert mod.extract_error(response) == "No provider available"

    def test_extract_error_dict_shape_falls_back_to_name(self):
        mod = _import_client()
        response = {"info": {"error": {"name": "APIError", "data": {}}}}
        assert mod.extract_error(response) == "APIError"

    def test_extract_error_string_shape(self):
        mod = _import_client()
        response = {"info": {"error": "boom"}}
        assert mod.extract_error(response) == "boom"
