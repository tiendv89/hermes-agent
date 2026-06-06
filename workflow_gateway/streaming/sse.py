"""Hermes callback → SSE event envelope translation."""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Dict, Optional

logger = logging.getLogger(__name__)

_WRITE_TOOL_ARTIFACTS: Dict[str, str] = {
    "workflow_write_product_spec": "product_spec",
    "workflow_write_technical_design": "technical_design",
}


def _sse(event_type: str, payload: Dict[str, Any]) -> str:
    return "data: " + json.dumps({"type": event_type, **payload}) + "\n\n"


class HermesSSETranslator:
    """Bridges synchronous AIAgent callbacks to an async SSE generator.

    Wire callbacks on AIAgent construction, then consume ``translator.stream()``
    as the StreamingResponse body.
    """

    def __init__(self) -> None:
        import asyncio

        self._loop = asyncio.get_running_loop()
        self._queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

    def _put(self, chunk: str) -> None:
        self._loop.call_soon_threadsafe(self._queue.put_nowait, chunk)

    def on_delta(self, delta: str, **_: Any) -> None:
        self._put(_sse("message_output_partial", {"content": delta}))

    def on_tool_start(self, name: str, call_id: str = "", **_: Any) -> None:
        self._put(_sse("tool_call_item", {"call_id": call_id or name, "name": name, "status": "running"}))

    def on_tool_complete(self, name: str, call_id: str = "", output: Any = None, **_: Any) -> None:
        self._put(_sse("function_call_output", {"call_id": call_id or name, "name": name, "output": output}))
        if name in _WRITE_TOOL_ARTIFACTS:
            artifact_output = output if isinstance(output, dict) else {}
            if artifact_output.get("ok", False):
                self._put(_sse("artifact_saved", {"artifact": _WRITE_TOOL_ARTIFACTS[name]}))

    def on_usage(self, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0, **_: Any) -> None:
        self._put(_sse("usage", {"input": input_tokens, "output": output_tokens, "cached": cached_tokens}))

    def on_error(self, message: str, **_: Any) -> None:
        self._put(_sse("error", {"message": message}))

    def done(self) -> None:
        self._put("data: [DONE]\n\n")
        self._put(None)

    async def stream(self) -> AsyncIterator[str]:
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk
