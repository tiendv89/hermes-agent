"""Adapt the agent's threaded callbacks into an async SSE chat stream.

A hermes ``AIAgent`` turn runs synchronously on a worker thread and reports
progress through callbacks; the FastAPI endpoint needs to *await* a stream of
Server-Sent Events on the event loop. :class:`HermesSSETranslator` sits between
them — callbacks push pre-rendered frames onto a loop-bound queue (crossing the
thread boundary with ``call_soon_threadsafe``), and :meth:`stream` drains that
queue as the ``StreamingResponse`` body.

The emitted format is hermes's native ``/v1/chat/completions`` SSE dialect, so
any OpenAI-compatible client can consume it:

    data: {"object": "chat.completion.chunk", "choices": [{"delta": {"role": "assistant"}, ...}]}
    data: {... "choices": [{"delta": {"content": "Hello"}, ...}]}
    data: {... "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {...}}
    data: [DONE]

On top of that base format two ``event:``-typed frames carry workflow-gateway
extensions (plain OpenAI clients ignore them):

    event: hermes.tool.progress   — a tool started / finished (status + toolCallId)
    event: hermes.artifact.saved  — a workflow write tool committed a document
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Dict, Optional

logger = logging.getLogger(__name__)

# Workflow write tools → the artifact kind the FE should refresh when they
# succeed. Drives the ``hermes.artifact.saved`` extension event.
ARTIFACT_BY_WRITE_TOOL: Dict[str, str] = {
    "write_product_spec": "product_spec",
    "write_technical_design": "technical_design",
}

# How long stream() waits with no agent activity before sending an SSE comment
# to keep the connection alive (slow tools shouldn't trip idle proxy timeouts).
# Comment frames (": ...") are ignored by SSE clients.
_KEEPALIVE_SECONDS = 15.0

# The end-of-stream sentinel placed on the queue by the closing frame emit.
_END = None


class HermesSSETranslator:
    """Translate AIAgent callbacks into an OpenAI-style SSE chat stream.

    Build it inside the request coroutine (it binds to the running loop), pass
    its ``on_*`` methods to the agent as callbacks, then return
    :meth:`stream` as the streaming response body. The agent thread calls the
    callbacks; the loop consumes :meth:`stream`.
    """

    def __init__(self, model: str = "hermes") -> None:
        self._loop = asyncio.get_running_loop()
        self._frames: "asyncio.Queue[Optional[str]]" = asyncio.Queue()
        self._model = model
        self._id = "chatcmpl-" + uuid.uuid4().hex
        self._created = int(time.time())
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._terminated = False

    # -- frame construction -------------------------------------------------

    def _chunk(self, delta: Dict[str, Any], **top_level: Any) -> str:
        """Render a ``chat.completion.chunk`` frame. ``top_level`` adds sibling keys
        (``finish_reason`` goes inside the choice; ``usage``/``hermes`` are siblings)."""
        finish_reason = top_level.pop("finish_reason", None)
        payload: Dict[str, Any] = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            **top_level,
        }
        return f"data: {json.dumps(payload)}\n\n"

    @staticmethod
    def _event(name: str, data: Dict[str, Any]) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    def _emit(self, frame: Optional[str]) -> None:
        """Push a rendered frame (or the end sentinel) from any thread onto the queue."""
        self._loop.call_soon_threadsafe(self._frames.put_nowait, frame)

    # -- AIAgent callbacks (called on the worker thread) --------------------

    def on_delta(self, delta: Any = None, **_: Any) -> None:
        # The agent fires a None delta to flush its display before a tool runs;
        # that's not text and not end-of-stream, so skip falsy deltas.
        if delta:
            self._emit(self._chunk({"content": delta}))

    def on_tool_start(self, call_id: str = "", name: str = "", args: Any = None, **_: Any) -> None:
        # hermes signature: tool_start_callback(call_id, name, args)
        self._emit(self._event("hermes.tool.progress", {
            "tool": name,
            "toolCallId": call_id or name,
            "status": "running",
        }))

    def on_tool_complete(
        self, call_id: str = "", name: str = "", args: Any = None, output: Any = None, **_: Any
    ) -> None:
        # hermes signature: tool_complete_callback(call_id, name, args, result)
        self._emit(self._event("hermes.tool.progress", {
            "tool": name,
            "toolCallId": call_id or name,
            "status": "completed",
        }))
        artifact = ARTIFACT_BY_WRITE_TOOL.get(name)
        if artifact and isinstance(output, dict) and output.get("ok"):
            self._emit(self._event("hermes.artifact.saved", {"artifact": artifact}))

    def on_usage(self, input_tokens: int = 0, output_tokens: int = 0, **_: Any) -> None:
        self._usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    def on_error(self, message: str, **_: Any) -> None:
        # Terminate with finish_reason="error"; the message rides in a hermes
        # sidecar so OpenAI parsers still accept the frame.
        self._terminate("error", error=message)

    def done(self) -> None:
        self._terminate("stop")

    # -- termination --------------------------------------------------------

    def _terminate(self, finish_reason: str, error: Optional[str] = None) -> None:
        """Emit the final chunk + ``[DONE]`` + end sentinel, at most once."""
        if self._terminated:
            return
        self._terminated = True

        siblings: Dict[str, Any] = {"finish_reason": finish_reason, "usage": self._usage}
        if error is not None:
            siblings["hermes"] = {"error": error}
        self._emit(self._chunk({}, **siblings))
        self._emit("data: [DONE]\n\n")
        self._emit(_END)

    # -- async consumption (runs on the event loop) -------------------------

    async def stream(self) -> AsyncIterator[str]:
        # Lead with the assistant role frame so clients open the message.
        yield self._chunk({"role": "assistant"})
        while True:
            try:
                frame = await asyncio.wait_for(self._frames.get(), _KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                yield ": keepalive\n\n"
                continue
            if frame is _END:
                return
            yield frame
