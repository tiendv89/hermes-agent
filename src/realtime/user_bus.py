"""In-process asyncio pub/sub bus for real-time per-user notification events.

Mirrors SessionBus (bus.py) but keyed by user_id instead of session_id — one
topic per user, fanning out every notification event (mention, channel
message, DM, stage approval, etc.) they're a recipient of, regardless of
which session/feature it came from. Powers the global toast + Activity-badge
push in the frontend via GET /api/v1/notifications/stream.

Same in-process, single-instance caveat as SessionBus: this does not fan out
across multiple backend processes. A future multi-process deployment needs a
Postgres LISTEN/NOTIFY or Redis pub/sub swap-in here.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Dict, List

_MAX_QUEUE = 256  # drop events for a slow subscriber rather than blocking


class UserBus:
    """Per-process in-memory pub/sub bus keyed by user id."""

    def __init__(self) -> None:
        self._topics: Dict[str, List[asyncio.Queue]] = {}

    def publish(self, user_id: str, event: Dict[str, Any]) -> None:
        """Publish an event to all current subscribers of ``user_id``.

        Non-blocking: subscribers that haven't drained their queue miss
        events (``put_nowait`` raises ``QueueFull``, silently dropped here).
        A disconnected/reconnecting client just falls back to the existing
        5s poll on the Activity feed for anything missed.
        """
        for q in list(self._topics.get(user_id, [])):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass

    def subscribe_raw(self, user_id: str, q: asyncio.Queue) -> None:
        """Register a pre-created queue synchronously (see SessionBus for why)."""
        self._topics.setdefault(user_id, []).append(q)

    def unsubscribe_raw(self, user_id: str, q: asyncio.Queue) -> None:
        """Unregister a queue previously registered via :meth:`subscribe_raw`."""
        subs = self._topics.get(user_id, [])
        try:
            subs.remove(q)
        except ValueError:
            pass
        if not subs:
            self._topics.pop(user_id, None)

    @asynccontextmanager
    async def subscribe(self, user_id: str) -> AsyncIterator[asyncio.Queue]:
        """Async context manager that registers a subscriber queue.

        Usage::

            async with user_bus.subscribe(user_id) as q:
                event = await q.get()
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=_MAX_QUEUE)
        self.subscribe_raw(user_id, q)
        try:
            yield q
        finally:
            self.unsubscribe_raw(user_id, q)


# ---------------------------------------------------------------------------
# Process-global singleton
# ---------------------------------------------------------------------------

_user_bus: UserBus | None = None


def get_user_bus() -> UserBus:
    """Return the process-global :class:`UserBus` instance."""
    global _user_bus
    if _user_bus is None:
        _user_bus = UserBus()
    return _user_bus
