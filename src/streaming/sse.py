"""Hermes callback → OpenAI chat-completions SSE translation.

Emits hermes's native ``/v1/chat/completions`` streaming wire format (see
``gateway/platforms/api_server.py``):

  * a role chunk first (``delta.role = "assistant"``),
  * ``chat.completion.chunk`` frames carrying ``delta.content`` for text,
  * ``event: hermes.tool.progress`` frames for tool start/complete
    (``status: running`` / ``status: completed``, correlated by ``toolCallId``),
  * a finish chunk (``finish_reason`` + ``usage``),
  * the ``data: [DONE]`` sentinel.

``event: hermes.artifact.saved`` is a workflow-gateway extension on top of the
native format — emitted when a workflow write tool succeeds — so the UI can
refresh feature artifacts. It is additive and ignored by generic OpenAI clients.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncIterator, Dict, Optional

logger = logging.getLogger(__name__)

_WRITE_TOOL_ARTIFACTS: Dict[str, str] = {
    "workflow_write_product_spec": "product_spec",
    "workflow_write_technical_design": "technical_design",
}

# Emit an SSE comment this often while the agent is busy (e.g. running a slow
# tool) and producing no events, so idle proxies/browsers don't drop the
# connection mid-turn. SSE comment lines (": ...") are ignored by clients.
_KEEPALIVE_SECONDS = 15.0


class HermesSSETranslator:
    """Bridges synchronous AIAgent callbacks to an async OpenAI-style SSE generator.

    Wire callbacks on AIAgent construction, then consume ``translator.stream()``
    as the StreamingResponse body.
    """

    def __init__(self, model: str = "hermes") -> None:
        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Optional[str]] = asyncio.Queue()
        self._model = model
        self._id = "chatcmpl-" + uuid.uuid4().hex
        self._created = int(time.time())
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self._finished = False

    # -- frame helpers ------------------------------------------------------

    def _chunk(
        self,
        delta: Dict[str, Any],
        finish_reason: Optional[str] = None,
        usage: Optional[Dict[str, int]] = None,
    ) -> str:
        obj: Dict[str, Any] = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
        if usage is not None:
            obj["usage"] = usage
        return "data: " + json.dumps(obj) + "\n\n"

    @staticmethod
    def _event(event: str, payload: Dict[str, Any]) -> str:
        return f"event: {event}\ndata: " + json.dumps(payload) + "\n\n"

    def _put(self, chunk: str) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, chunk)

    # -- AIAgent callbacks --------------------------------------------------

    def on_delta(self, delta: Any, **_: Any) -> None:
        # hermes fires stream_delta_callback(None) as a "flush/close the current
        # display box" sentinel before tool execution. It is NOT end-of-stream
        # and carries no text — drop it so we don't emit an empty content chunk.
        if not delta:
            return
        self._put(self._chunk({"content": delta}))

    def on_tool_start(
        self, call_id: str = "", name: str = "", args: Any = None, **_: Any
    ) -> None:
        # Matches hermes tool_executor: tool_start_callback(call_id, name, args).
        self._put(self._event("hermes.tool.progress", {
            "tool": name,
            "toolCallId": call_id or name,
            "status": "running",
        }))

    def on_tool_complete(
        self, call_id: str = "", name: str = "", args: Any = None, output: Any = None, **_: Any
    ) -> None:
        # Matches hermes tool_executor: tool_complete_callback(call_id, name, args, result).
        self._put(self._event("hermes.tool.progress", {
            "tool": name,
            "toolCallId": call_id or name,
            "status": "completed",
        }))
        if name in _WRITE_TOOL_ARTIFACTS:
            artifact_output = output if isinstance(output, dict) else {}
            if artifact_output.get("ok", False):
                self._put(self._event("hermes.artifact.saved", {
                    "artifact": _WRITE_TOOL_ARTIFACTS[name],
                }))

    def on_usage(self, input_tokens: int = 0, output_tokens: int = 0, **_: Any) -> None:
        self._usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }

    def on_error(self, message: str, **_: Any) -> None:
        # Surface the error on the finish chunk (finish_reason="error") with a
        # hermes-namespaced extra so the client can distinguish it from a clean
        # stop without breaking OpenAI-compatible parsers.
        self._finalize("error", error=message)

    def done(self) -> None:
        self._finalize("stop")

    # -- termination --------------------------------------------------------

    def _finalize(self, finish_reason: str, error: Optional[str] = None) -> None:
        """Emit the finish chunk + [DONE] sentinel exactly once."""
        if self._finished:
            return
        self._finished = True
        obj: Dict[str, Any] = {
            "id": self._id,
            "object": "chat.completion.chunk",
            "created": self._created,
            "model": self._model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
            "usage": self._usage,
        }
        if error:
            obj["hermes"] = {"error": error}
        self._put("data: " + json.dumps(obj) + "\n\n")
        self._put("data: [DONE]\n\n")
        self._put(None)

    async def stream(self) -> AsyncIterator[str]:
        # Lead with the OpenAI role chunk so clients open the assistant message.
        yield self._chunk({"role": "assistant"})
        while True:
            try:
                chunk = await asyncio.wait_for(self._queue.get(), timeout=_KEEPALIVE_SECONDS)
            except asyncio.TimeoutError:
                # No agent activity for a while (e.g. a slow tool) — keep the
                # connection warm so it isn't dropped before the answer arrives.
                yield ": keepalive\n\n"
                continue
            if chunk is None:
                return
            yield chunk
