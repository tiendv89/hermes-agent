"""SSE translator that also fans out structured events to the in-process bus.

The agent turn still streams back the OpenAI-compatible SSE format (via the
inherited :class:`HermesSSETranslator`) for the legacy ``/chat`` shim, while
simultaneously publishing structured bus events so every ``GET .../stream``
subscriber sees the agent output live (§4.3 / T3).

Bus event schema
----------------
Each event is a dict ``{"event": "<name>", "data": {...}}``:

    agent.delta             — {"content": "<text token>"}
    hermes.tool.progress    — {"tool": "...", "toolCallId": "...", "status": "running"|"completed"}
    hermes.artifact.saved   — {"artifact": "<kind>"}
    agent.done              — {"finish_reason": "stop"|"error", "error": "<msg>"}  (error optional)
    agent.working           — {"session_id": "..."}  (published when the turn starts)
"""

from __future__ import annotations

from typing import Any

from src.realtime.bus import get_bus
from src.streaming.sse import HermesSSETranslator, artifact_for_tool, coerce_tool_output


class BusPublishingSSETranslator(HermesSSETranslator):
    """HermesSSETranslator extended to fan out structured events on the bus."""

    def __init__(self, session_id: str, model: str = "hermes") -> None:
        super().__init__(model=model)
        self._session_id = session_id
        self._full_parts: list[str] = []

    @property
    def full_text(self) -> str:
        """The assistant reply accumulated from streamed deltas (for persistence backfill)."""
        return "".join(self._full_parts)

    def _bus_publish(self, event: str, data: dict) -> None:
        # Callbacks are invoked from a worker thread (run_in_executor); use
        # call_soon_threadsafe to schedule put_nowait on the event-loop thread.
        bus = get_bus()
        self._loop.call_soon_threadsafe(
            bus.publish, self._session_id, {"event": event, "data": data}
        )

    # -- overridden callbacks -----------------------------------------------

    def on_delta(self, delta: Any = None, **kwargs: Any) -> None:
        super().on_delta(delta=delta, **kwargs)
        if delta:
            self._full_parts.append(str(delta))
            self._bus_publish("agent.delta", {"content": delta})

    def on_tool_start(
        self, call_id: str = "", name: str = "", args: Any = None, **kwargs: Any
    ) -> None:
        super().on_tool_start(call_id=call_id, name=name, args=args, **kwargs)
        self._bus_publish(
            "hermes.tool.progress",
            {"tool": name, "toolCallId": call_id or name, "status": "running"},
        )

    def on_tool_complete(
        self,
        call_id: str = "",
        name: str = "",
        args: Any = None,
        output: Any = None,
        **kwargs: Any,
    ) -> None:
        super().on_tool_complete(
            call_id=call_id, name=name, args=args, output=output, **kwargs
        )
        self._bus_publish(
            "hermes.tool.progress",
            {"tool": name, "toolCallId": call_id or name, "status": "completed"},
        )
        artifact = artifact_for_tool(name, args, output)
        if artifact and coerce_tool_output(output).get("ok"):
            self._bus_publish("hermes.artifact.saved", {"artifact": artifact})

    def on_error(self, message: str = "", **kwargs: Any) -> None:
        super().on_error(message=message, **kwargs)
        self._bus_publish("agent.done", {"finish_reason": "error", "error": message})

    def done(self) -> None:
        super().done()
        self._bus_publish("agent.done", {"finish_reason": "stop"})
