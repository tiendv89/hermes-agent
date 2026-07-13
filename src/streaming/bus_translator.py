"""SSE translator that also fans out structured events to the in-process bus.

The agent turn still streams back the OpenAI-compatible SSE format (via the
inherited :class:`HermesSSETranslator`) for the legacy ``/chat`` shim, while
simultaneously publishing structured bus events so every ``GET .../stream``
subscriber sees the agent output live (§4.3 / T3).

Bus event schema
----------------
Each event is a dict ``{"event": "<name>", "data": {...}}``:

    agent.delta             — {"content": "<text token>"}
    agent.reasoning         — {"content": "<reasoning delta>"}
    hermes.tool.progress    — {"tool": "...", "toolCallId": "...", "status": "running"|"completed"}
    hermes.artifact.saved   — {"artifact": "<kind>"}
    agent.done              — {"finish_reason": "stop"|"error", "error": "<msg>"}  (error optional)
    agent.working           — {"session_id": "..."}  (published when the turn starts)

Published elsewhere in the turn lifecycle (src/api/agent_dispatch.py), not by
this translator, but part of the same live-event surface:

    message.thread_updated  — {"session_id": "...", "root_message_id": "<id>",
                                "thread_summary": {"reply_count": N, "recent_repliers": [...]}}
                              (published once a threaded reply is persisted —
                              the live counterpart of the thread_summary a
                              reload attaches via GET .../messages)
"""

from __future__ import annotations

from typing import Any

from src.realtime.bus import get_bus
from src.streaming.sse import HermesSSETranslator, artifact_for_tool, coerce_tool_output


class BusPublishingSSETranslator(HermesSSETranslator):
    """HermesSSETranslator extended to fan out structured events on the bus."""

    def __init__(
        self,
        session_id: str,
        model: str = "hermes",
        thread_root_id: int | None = None,
    ) -> None:
        super().__init__(model=model)
        self._session_id = session_id
        self._thread_root_id = thread_root_id

    def _bus_publish(self, event: str, data: dict) -> None:
        # Callbacks are invoked from a worker thread (run_in_executor); use
        # call_soon_threadsafe to schedule put_nowait on the event-loop thread.
        if self._thread_root_id is not None:
            # Stringified to match message.created's thread_root_id convention
            # (src/api/routers/messages.py) — a frontend correlating a live
            # agent.* event against an existing message by this id would
            # otherwise compare a raw int here against a string everywhere else.
            data = {**data, "thread_root_id": str(self._thread_root_id)}
        bus = get_bus()
        self._loop.call_soon_threadsafe(
            bus.publish, self._session_id, {"event": event, "data": data}
        )

    # -- overridden callbacks -----------------------------------------------

    def on_reasoning(self, delta: Any = None, **kwargs: Any) -> None:
        if self._stopped:
            return
        super().on_reasoning(delta=delta, **kwargs)
        if delta:
            self._bus_publish("agent.reasoning", {"content": str(delta)})

    def on_delta(self, delta: Any = None, **kwargs: Any) -> None:
        if self._stopped:
            return
        super().on_delta(delta=delta, **kwargs)
        if delta:
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
        if not self._stopped:
            self._bus_publish(
                "agent.done", {"finish_reason": "error", "error": message}
            )

    def done(self) -> None:
        if self._stopped:
            return
        super().done()
        self._bus_publish("agent.done", {"finish_reason": "stop"})
