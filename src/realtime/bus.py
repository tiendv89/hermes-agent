"""In-process asyncio pub/sub bus for real-time session events.

Each session (thread or channel) has a topic identified by its session id.
Publishers call ``publish(session_id, event)``; subscribers receive events via
an ``asyncio.Queue`` obtained from ``subscribe(session_id)``.

Design note (§3.3 / T3): this module is the seam between the in-process v4
implementation and a future Postgres LISTEN/NOTIFY or Redis replacement.
T3 expands this bus with ``GET …/threads/{id}/stream`` SSE subscription;
T4 uses it only to publish ``channel.deleted`` events on channel deletion.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List

_MAX_QUEUE = 256  # drop events for a slow subscriber rather than blocking


class SessionBus:
    """Per-process in-memory pub/sub bus keyed by session id."""

    def __init__(self) -> None:
        self._topics: Dict[str, List[asyncio.Queue]] = {}

    def publish(self, session_id: str, event: Dict[str, Any]) -> None:
        """Publish an event to all current subscribers of ``session_id``.

        Non-blocking: subscribers that have not drained their queue will miss
        events (their ``put_nowait`` raises ``QueueFull`` which is silently
        dropped here). This matches the in-process v4 delivery contract — a
        ``?since=`` replay cursor (T3) covers the gap on reconnect.
        """
        for q in list(self._topics.get(session_id, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe_raw(self, session_id: str, q: asyncio.Queue) -> None:
        """Register a pre-created queue synchronously.

        Use when the subscription must be registered before an async operation
        (e.g. a DB fetch) to avoid a race window. The caller is responsible for
        cleanup via :meth:`unsubscribe_raw`.
        """
        self._topics.setdefault(session_id, []).append(q)

    def unsubscribe_raw(self, session_id: str, q: asyncio.Queue) -> None:
        """Unregister a queue previously registered via :meth:`subscribe_raw`."""
        subs = self._topics.get(session_id, [])
        try:
            subs.remove(q)
        except ValueError:
            pass
        if not subs:
            self._topics.pop(session_id, None)

    @asynccontextmanager
    async def subscribe(self, session_id: str) -> AsyncIterator[asyncio.Queue]:
        """Async context manager that registers a subscriber queue.

        Usage::

            async with bus.subscribe(session_id) as q:
                event = await q.get()
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
        self.subscribe_raw(session_id, q)
        try:
            yield q
        finally:
            self.unsubscribe_raw(session_id, q)


# ---------------------------------------------------------------------------
# Process-global singleton
# ---------------------------------------------------------------------------

_bus: SessionBus | None = None


def get_bus() -> SessionBus:
    """Return the process-global :class:`SessionBus` instance."""
    global _bus
    if _bus is None:
        _bus = SessionBus()
    return _bus
