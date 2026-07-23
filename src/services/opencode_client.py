"""HTTP client for an opencode server (github.com/anomalyco/opencode).

hermes-agent calls a gateway-operated ``opencode serve`` instance to run
coding-verdict turns for IDE-originated /chat calls (see
``src/api/agent_dispatch.py::_run_opencode_turn``) — see
``src/mcp/coding_bridge_server.py`` for the MCP server opencode's own agent
loop is pointed at for file/git/terminal tool execution (the IDE extension
does the actual work on the developer's machine), and ``src/api/triage.py``
for the per-turn classifier that decides a turn is coding rather than
doc/chat.

Configuration (env vars), following the ``<NAME>_SERVICE_URL``/
``<NAME>_SERVICE_TOKEN`` convention used by ``vcs_service_client.py`` /
``workflow_backend_client.py``:

  OPENCODE_SERVER_URL       Base URL of the opencode server, e.g.
                             http://localhost:4096. If unset, every call
                             raises OpencodeClientError(reason_code=
                             "missing_config").
  OPENCODE_SERVER_PASSWORD  Optional HTTP Basic Auth password. Not a
                             documented ``opencode serve --help`` flag —
                             opencode reads this (and OPENCODE_SERVER_USERNAME
                             below) directly from its own process environment,
                             confirmed via its own startup log ("Warning:
                             OPENCODE_SERVER_PASSWORD is not set; server is
                             unsecured") rather than --help, which lists
                             neither. Omit both for a server running
                             unsecured behind this gateway's own network
                             perimeter.
  OPENCODE_SERVER_USERNAME  Basic Auth username, paired with the password
                             above. Only meaningful once a password is set —
                             confirmed live that opencode REJECTS an empty
                             Basic-Auth username even with the correct
                             password, so this must exactly match whatever
                             opencode's own process sees for this same env
                             var (both this client and the opencode process
                             are launched from the same .env via this repo's
                             Makefile, so in practice they always agree).

Endpoint contracts (opencode server, confirmed against a live v1.18.4
instance — see the OpenAPI spec at GET /doc):

  POST /session                        -> {"id": "ses_...", ...}
  POST /mcp                            body {"name", "config": {"type":
                                        "local"|"remote", ...}} -> per-name
                                        connection status
  POST /session/{id}/message           body {"model": {"providerID",
                                        "modelID"}, "agent"?, "tools"?,
                                        "variant"?, "parts":
                                        [{"type":"text","text":...}]} —
                                        "variant" selects a named model
                                        variant (confirmed via GET
                                        /config/providers, e.g.
                                        deepseek-v4-pro: "high"/"max") —
                                        omitting it does not disable
                                        reasoning, it just leaves opencode's
                                        own default in effect, which isn't
                                        necessarily a high-effort one.
                                        -> {"info": AssistantMessage,
                                        "parts": Part[]}, BLOCKS until the
                                        turn (including any MCP tool round
                                        trips) completes.

  NOTE: this client uses the blocking POST, not real-time SSE consumption
  of GET /session/{id}/event — the caller gets one accumulated response
  once the whole turn finishes, not token-by-token deltas the way the
  Hermes-backed path streams. This is a known, deliberate scope cut for
  the first opencode integration (see agent_dispatch.py's
  ``_run_opencode_turn``) — real-time streaming is a fast-follow once this
  path works end-to-end, not a silent regression to hide.

  All endpoints: 4xx/5xx -> body is opencode's own JSON-RPC-ish error shape;
  surfaced here as OpencodeClientError with the raw body text.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

_DEFAULT_TURN_TIMEOUT_SECONDS = 300


class OpencodeClientError(Exception):
    """Raised when the opencode server returns a non-2xx response or is misconfigured.

    Attributes:
        reason_code: ``"missing_config"`` for local config errors, empty for
            ordinary HTTP errors (opencode's error body isn't a stable
            machine-readable code today).
        status: HTTP status code, 0 when the error is local (not from HTTP).
    """

    def __init__(self, message: str, *, reason_code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status


def check_opencode_available() -> bool:
    """Return True only when OPENCODE_SERVER_URL is configured."""
    return bool(os.environ.get("OPENCODE_SERVER_URL", "").strip())


def _resolve_config() -> tuple[str, Optional[str], Optional[str]]:
    base_url = os.environ.get("OPENCODE_SERVER_URL", "").rstrip("/")
    if not base_url:
        raise OpencodeClientError(
            "OPENCODE_SERVER_URL is not set — cannot call the opencode server.",
            reason_code="missing_config",
        )
    password = os.environ.get("OPENCODE_SERVER_PASSWORD", "") or None
    # opencode itself reads OPENCODE_SERVER_USERNAME from its own environment
    # (same .env, since it's launched via this repo's Makefile, which exports
    # every var into every target) — confirmed live: once a password is set,
    # an empty Basic-Auth username is REJECTED, so this must mirror whatever
    # opencode's own process actually sees, not a value invented here.
    username = os.environ.get("OPENCODE_SERVER_USERNAME", "")
    return base_url, username, password


def _auth(username: Optional[str], password: Optional[str]) -> Optional[aiohttp.BasicAuth]:
    if not password:
        return None
    return aiohttp.BasicAuth(login=username or "", password=password)


async def _request(
    method: str,
    path: str,
    *,
    json_body: Optional[Dict[str, Any]] = None,
    timeout: int = 30,
) -> Dict[str, Any]:
    """Shared HTTP plumbing for opencode server calls in this module."""
    base_url, username, password = _resolve_config()
    url = f"{base_url}{path}"

    async with aiohttp.ClientSession() as session:
        async with session.request(
            method,
            url,
            json=json_body,
            auth=_auth(username, password),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = {"raw": await resp.text()}

            if 200 <= resp.status < 300:
                return data if isinstance(data, dict) else {"result": data}

            msg = f"opencode server returned HTTP {resp.status} for {method} {url}: {str(data)[:500]}"
            logger.warning(msg)
            raise OpencodeClientError(msg, status=resp.status)


async def create_session(*, title: str = "", agent: str = "") -> str:
    """Create a new opencode session and return its id (``ses_...``)."""
    payload: Dict[str, Any] = {}
    if title:
        payload["title"] = title
    if agent:
        payload["agent"] = agent
    data = await _request("POST", "/session", json_body=payload)
    session_id = data.get("id", "")
    if not session_id:
        raise OpencodeClientError(f"opencode /session response missing 'id': {data!r}")
    return session_id


async def register_mcp_bridge(name: str, url: str, *, timeout_ms: int = 30000) -> None:
    """Point opencode at a remote MCP server (our coding_bridge_server mount).

    Raises OpencodeClientError if opencode reports the connection failed —
    callers should treat that as fatal for the turn (opencode would fall
    back to its own built-in file/shell tools otherwise, silently bypassing
    the IDE-deferred execution model entirely).
    """
    data = await _request(
        "POST",
        "/mcp",
        json_body={
            "name": name,
            "config": {
                "type": "remote",
                "url": url,
                "enabled": True,
                "timeout": timeout_ms,
                "oauth": False,
            },
        },
    )
    status = (data.get(name) or {}).get("status")
    if status != "connected":
        raise OpencodeClientError(
            f"opencode MCP registration for {name!r} did not connect: {data!r}"
        )


async def send_message(
    session_id: str,
    text: str,
    *,
    provider_id: str,
    model_id: str,
    agent: str = "",
    tools: Optional[Dict[str, bool]] = None,
    system: str = "",
    variant: str = "",
    timeout: int = _DEFAULT_TURN_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    """Send a message and block until the turn (incl. any MCP tool calls) completes.

    ``variant`` selects a named model variant (confirmed via a live server's
    GET /config/providers, e.g. deepseek-v4-pro exposes "high"/"max",
    claude-sonnet-5 exposes "low"/"medium"/"high"/"xhigh"/"max") — this is
    opencode's reasoning-effort knob; omitting it does not mean "no
    reasoning config", it means opencode picks its own default, which is
    not necessarily any of the model's higher-effort variants. Passed
    through as-is (no validation against the model's actual variant list —
    callers are expected to pass a value that exists across the models they
    use, e.g. "high", which every reasoning-capable model above exposes).

    Returns the raw ``{"info": AssistantMessage, "parts": Part[]}`` response.
    See ``extract_text``/``extract_usage`` to pull out what callers need.
    """
    payload: Dict[str, Any] = {
        "model": {"providerID": provider_id, "modelID": model_id},
        "parts": [{"type": "text", "text": text}],
    }
    if agent:
        payload["agent"] = agent
    if tools:
        payload["tools"] = tools
    if system:
        payload["system"] = system
    if variant:
        payload["variant"] = variant
    return await _request(
        "POST", f"/session/{session_id}/message", json_body=payload, timeout=timeout
    )


def extract_text(response: Dict[str, Any]) -> str:
    """Concatenate every ``text`` part's content from a send_message response."""
    parts: List[Dict[str, Any]] = response.get("parts") or []
    return "".join(p.get("text", "") for p in parts if p.get("type") == "text")


def extract_reasoning(response: Dict[str, Any]) -> str:
    """Concatenate every ``reasoning`` part's content from a send_message response."""
    parts: List[Dict[str, Any]] = response.get("parts") or []
    return "".join(p.get("text", "") for p in parts if p.get("type") == "reasoning")


def extract_usage(response: Dict[str, Any]) -> Dict[str, int]:
    """Pull token/cost usage out of the response's ``info`` (AssistantMessage).

    Shape confirmed against opencode's OpenAPI spec: ``info.tokens.{input,
    output, reasoning, cache: {read, write}}`` and ``info.cost`` — a direct
    match for ``src.services.cost_client.emit_turn_cost``'s parameters.
    """
    info = response.get("info") or {}
    tokens = info.get("tokens") or {}
    cache = tokens.get("cache") or {}
    return {
        "input_tokens": int(tokens.get("input") or 0),
        "output_tokens": int(tokens.get("output") or 0),
        "cache_read_tokens": int(cache.get("read") or 0),
        "cache_write_tokens": int(cache.get("write") or 0),
    }


def extract_error(response: Dict[str, Any]) -> Optional[str]:
    """Return a human-readable error message if the turn's AssistantMessage failed."""
    info = response.get("info") or {}
    error = info.get("error")
    if not error:
        return None
    if isinstance(error, dict):
        data = error.get("data") or {}
        return data.get("message") or error.get("name") or str(error)
    return str(error)


def run_async(coro):
    """Bridge an async opencode_client coroutine into a sync call.

    Mirrors src.services.vcs_service_client.run_async — uses the running
    agent event loop when available (production path, scheduled cross-thread
    via run_coroutine_threadsafe), else falls back to asyncio.run() for tests
    and non-agent callers.
    """
    import asyncio

    from plugins.context import get_agent_loop

    loop = get_agent_loop()
    if loop is not None and loop.is_running():
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result(timeout=_DEFAULT_TURN_TIMEOUT_SECONDS)
    return asyncio.run(coro)
