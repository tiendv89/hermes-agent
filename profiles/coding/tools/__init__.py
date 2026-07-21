"""Coding profile tools — client-executed (deferred) schemas and handlers.

Every tool in this package returns a deferred-execution marker dict
(``{"__deferred__": True, "tool": "<name>", "params": {...}}``) instead
of executing anything server-side. The IDE extension receives the marker
via SSE, executes the tool locally, and returns the result.

Tools are grouped into three modules:
- ``local_file_ops`` — read_file, edit_file, write_file, create_directory,
  browse_directory, search_code, search_files
- ``terminal`` — run_command
- ``git_ops`` — git_status, git_diff, git_commit, git_push, git_checkout,
  git_log

Handler convention
------------------
Every handler is a sync function with signature
``handle_<tool>(**kwargs) -> dict`` that:
1. Validates required parameters.
2. Builds a ``params`` dict from the keyword arguments.
3. Returns ``{"__deferred__": True, "tool": "<tool_name>", "params": {...}}``.

The ``__deferred__`` key signals the tool-registration infrastructure to
skip guardrail sanitization and pass the result through unmodified.  The
SSE translator (T4) picks up this marker and emits a
``hermes.tool.deferred`` event instead of a normal tool result.
"""

from __future__ import annotations

from typing import Any


def deferred(tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """Return a deferred-execution marker dict for *tool* with *params*."""
    return {"__deferred__": True, "tool": tool, "params": params}
