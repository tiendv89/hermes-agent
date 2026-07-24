"""Deferred-execution marker helper for IDE (client-executed) tools.

git_ops.py, local_file_ops.py, and terminal.py in this package return this
marker dict (``{"__deferred__": True, "tool": "<name>", "params": {...}}``)
instead of executing anything server-side. The IDE extension receives the
marker via SSE, executes the tool locally, and returns the result.

The ``__deferred__`` key signals the tool-registration infrastructure
(``plugins/__init__.py``) to skip guardrail sanitization and pass the
result through unmodified — the SSE translator picks up this marker and
emits a ``hermes.tool.deferred`` event instead of a normal tool result.
"""

from __future__ import annotations

from typing import Any


def deferred(tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """Return a deferred-execution marker dict for *tool* with *params*."""
    return {"__deferred__": True, "tool": tool, "params": params}
