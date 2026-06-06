"""Human-in-loop approval registry stub for workflow gateway v1.

In v1 no tool calls require approval — all execute immediately. A future
iteration can register approval callbacks here (e.g. require human
confirmation before write_product_spec mutates a file).
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional


class ApprovalRegistry:
    """Tracks tool calls that require human approval before execution."""

    def __init__(self) -> None:
        self._pending: Dict[str, Dict[str, Any]] = {}

    def requires_approval(self, tool_name: str) -> bool:
        """Return True if tool_name is gated behind human approval. v1: always False."""
        return False

    def register_callback(self, call_id: str, meta: Dict[str, Any], callback: Callable) -> None:
        """Register a pending approval. callback(approved: bool) is called when resolved."""
        self._pending[call_id] = {"meta": meta, "callback": callback}

    def resolve(self, call_id: str, approved: bool) -> bool:
        """Resolve a pending approval. Returns True if the call_id was found."""
        entry = self._pending.pop(call_id, None)
        if entry is None:
            return False
        entry["callback"](approved)
        return True


# Module-level singleton used by the router.
registry = ApprovalRegistry()
