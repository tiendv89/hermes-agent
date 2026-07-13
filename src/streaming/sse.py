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
    "write_tasks": "tasks",
    "write_file": "generic_file",
    "edit_file": "generic_file",
}


def coerce_tool_output(output: Any) -> Dict[str, Any]:
    """Return the tool result as a dict.

    Plugin handlers are wrapped by _json_result_handler, so the result that
    reaches the tool-complete callback is a JSON *string* (e.g. '{"ok": true,
    "path": "..."}'), not a dict. Parse it so artifact detection / the ok-check
    work. Returns {} when it isn't a JSON object.
    """
    if isinstance(output, dict):
        return output
    if isinstance(output, str):
        try:
            parsed = json.loads(output)
            return parsed if isinstance(parsed, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def artifact_for_tool(name: str, args: Any = None, output: Any = None) -> Optional[str]:
    """Resolve which artifact a completed tool call wrote, for the FE refresh event.

    Robust to toolset-prefixed names (``workflow_write_product_spec``), the
    ``edit_document`` tool (whose target is in its args, not output), and falls
    back to the committed file path in the output. Returns None when the tool
    didn't write a feature document.
    """
    base = name[len("workflow_") :] if name.startswith("workflow_") else name
    artifact = ARTIFACT_BY_WRITE_TOOL.get(base) or ARTIFACT_BY_WRITE_TOOL.get(name)
    if artifact:
        return artifact
    # edit_document targets product_spec / technical_design via its args.
    if base in ("edit_document",) and isinstance(args, dict):
        doc = args.get("document")
        if doc in ("product_spec", "technical_design"):
            return doc
    # Last resort: infer from the committed file path in the tool result.
    out = coerce_tool_output(output)
    path = str(out.get("path") or "")
    if path.endswith("product-spec.md"):
        return "product_spec"
    if path.endswith("technical-design.md"):
        return "technical_design"
    if path.endswith("tasks.md"):
        return "tasks"
    return None


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
        self._stopped = False
        self._full_parts: list = []
        self._reasoning_parts: list = []

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

    def mark_stopped(self) -> str:
        """Signal that the turn was cancelled. Returns accumulated text and suppresses done().

        Called from the cancel handler (async context) before the thread's done() fires.
        Thread-safe: sets a flag that done() checks before emitting the terminal frame.
        """
        self._stopped = True
        return "".join(self._full_parts)

    @property
    def full_text(self) -> str:
        """Accumulated assistant text from streamed deltas."""
        return "".join(self._full_parts)

    def on_delta(self, delta: Any = None, **_: Any) -> None:
        # The agent fires a None delta to flush its display before a tool runs;
        # that's not text and not end-of-stream, so skip falsy deltas.
        if delta:
            self._full_parts.append(str(delta))
            self._emit(self._chunk({"content": delta}))

    def on_reasoning(self, delta: Any = None, **_: Any) -> None:
        """Emit an ephemeral agent.reasoning frame for a reasoning-trace delta.

        Reasoning is accumulated separately from assistant content — it is never
        appended to self._full_parts and never persisted. Falsy deltas (the
        model's flush sentinel) are silently skipped.
        """
        if delta:
            self._reasoning_parts.append(str(delta))
            self._emit(
                self._event(
                    "agent.reasoning",
                    {"object": "reasoning.delta", "content": str(delta)},
                )
            )

    def on_tool_start(
        self, call_id: str = "", name: str = "", args: Any = None, **_: Any
    ) -> None:
        # hermes signature: tool_start_callback(call_id, name, args)
        self._emit(
            self._event(
                "hermes.tool.progress",
                {
                    "tool": name,
                    "toolCallId": call_id or name,
                    "status": "running",
                },
            )
        )

    def on_tool_complete(
        self,
        call_id: str = "",
        name: str = "",
        args: Any = None,
        output: Any = None,
        **_: Any,
    ) -> None:
        # hermes signature: tool_complete_callback(call_id, name, args, result)
        self._emit(
            self._event(
                "hermes.tool.progress",
                {
                    "tool": name,
                    "toolCallId": call_id or name,
                    "status": "completed",
                },
            )
        )
        artifact = artifact_for_tool(name, args, output)
        if artifact and coerce_tool_output(output).get("ok"):
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
        """Emit the final chunk + ``[DONE]`` + end sentinel, at most once.

        Suppressed after mark_stopped() so a still-running thread does not
        emit a stale agent.done / [DONE] frame after cancellation.
        """
        if self._terminated or self._stopped:
            return
        self._terminated = True

        # Emit reasoning.done before the finish frame when any reasoning was streamed,
        # giving the FE a clean signal to collapse the thinking area.
        if self._reasoning_parts:
            self._emit(
                self._event("agent.reasoning", {"object": "reasoning.done"})
            )

        siblings: Dict[str, Any] = {
            "finish_reason": finish_reason,
            "usage": self._usage,
        }
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
            # Let the event loop drain the write buffer before the next frame.
            # Prevents uvicorn from batching multiple rapid delta frames into one
            # TCP segment, which makes the UI render text in large chunks.
            await asyncio.sleep(0)
