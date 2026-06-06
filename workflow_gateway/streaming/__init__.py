"""Hermes callback → SSE event envelope translation.

Translates AIAgent callbacks into the voyager SSE event envelope used by
voyager-interface and digital-factory-ui.

Event types:
    message_output_partial  — streamed text delta
    tool_call_item          — tool invocation started
    function_call_output    — tool result
    artifact_saved          — write-tool succeeded (emitted after function_call_output)
    usage                   — token counts at turn end
    error                   — stream error
    ignored                 — concurrent stream blocked
    [DONE]                  — end of stream sentinel

Wire format (SSE):
    data: {"type": "<event_type>", ...payload}\n\n
"""

from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Names of write tools that emit an artifact_saved event on success.
_WRITE_TOOL_ARTIFACTS: Dict[str, str] = {
    "workflow_write_product_spec": "product_spec",
    "workflow_write_technical_design": "technical_design",
}


def _sse(event_type: str, payload: Dict[str, Any]) -> str:
    """Encode a single SSE data line."""
    return "data: " + json.dumps({"type": event_type, **payload}) + "\n\n"


class HermesSSETranslator:
    """Collects AIAgent callbacks and exposes them as an async SSE generator.

    Usage::

        translator = HermesSSETranslator()
        agent = AIAgent(
            stream_delta_callback=translator.on_delta,
            tool_start_callback=translator.on_tool_start,
            tool_complete_callback=translator.on_tool_complete,
            status_callback=translator.on_status,
            ...
        )
        async for chunk in translator.stream():
            yield chunk
    """

    def __init__(self) -> None:
        import asyncio

        self._queue: asyncio.Queue[Optional[str]] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Callbacks — called from the agent thread (may be synchronous)
    # ------------------------------------------------------------------

    def _put(self, chunk: str) -> None:
        """Thread-safe enqueue of an SSE chunk."""
        import asyncio

        loop = asyncio.get_event_loop()
        if loop.is_running():
            loop.call_soon_threadsafe(self._queue.put_nowait, chunk)
        else:
            self._queue.put_nowait(chunk)

    def on_delta(self, delta: str, **_kwargs: Any) -> None:
        """Handle a streaming text delta from the LLM."""
        self._put(_sse("message_output_partial", {"content": delta}))

    def on_tool_start(self, name: str, call_id: str = "", **_kwargs: Any) -> None:
        """Handle a tool invocation starting."""
        self._put(
            _sse(
                "tool_call_item",
                {"call_id": call_id or name, "name": name, "status": "running"},
            )
        )

    def on_tool_complete(
        self,
        name: str,
        call_id: str = "",
        output: Any = None,
        **_kwargs: Any,
    ) -> None:
        """Handle a tool result."""
        self._put(
            _sse(
                "function_call_output",
                {"call_id": call_id or name, "name": name, "output": output},
            )
        )
        # Emit artifact_saved for write tools that succeeded.
        if name in _WRITE_TOOL_ARTIFACTS:
            artifact_output = output if isinstance(output, dict) else {}
            if artifact_output.get("ok", False):
                self._put(
                    _sse(
                        "artifact_saved",
                        {"artifact": _WRITE_TOOL_ARTIFACTS[name]},
                    )
                )

    def on_usage(self, input_tokens: int = 0, output_tokens: int = 0, cached_tokens: int = 0, **_kwargs: Any) -> None:
        """Handle token usage at end of turn."""
        self._put(
            _sse(
                "usage",
                {"input": input_tokens, "output": output_tokens, "cached": cached_tokens},
            )
        )

    def on_error(self, message: str, **_kwargs: Any) -> None:
        """Handle a stream error."""
        self._put(_sse("error", {"message": message}))

    def done(self) -> None:
        """Signal end of stream."""
        self._put("data: [DONE]\n\n")
        self._put(None)  # sentinel to stop the async generator

    # ------------------------------------------------------------------
    # Async generator — consumed by the FastAPI SSE endpoint
    # ------------------------------------------------------------------

    async def stream(self) -> AsyncIterator[str]:
        """Yield SSE chunks until the done sentinel is received."""
        while True:
            chunk = await self._queue.get()
            if chunk is None:
                return
            yield chunk
