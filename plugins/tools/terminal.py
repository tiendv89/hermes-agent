"""Coding-profile terminal tool — deferred execution.

The ``run_command`` handler returns a deferred-execution marker; the IDE
extension executes the command in the developer's real terminal and returns
stdout, stderr, and exit code.
"""

from __future__ import annotations

from typing import Any

from plugins.tools.deferred import deferred

# ---------------------------------------------------------------------------
# schema
# ---------------------------------------------------------------------------

RUN_COMMAND_SCHEMA: dict[str, Any] = {
    "description": (
        "Run a shell command in the developer's IDE terminal. "
        "Returns stdout, stderr, and exit code. "
        "The command runs in the workspace root directory by default."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "Shell command to execute (e.g. 'pytest', 'npm test').",
            },
            "workdir": {
                "type": "string",
                "description": "Working directory relative to workspace root. "
                "Defaults to the workspace root.",
            },
            "timeout": {
                "type": "integer",
                "description": "Max execution time in seconds. Defaults to the "
                "extension's configured timeout (typically 300s).",
            },
        },
        "required": ["command"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# handler
# ---------------------------------------------------------------------------


def handle_run_command(
    command: str = "",
    workdir: str = "",
    timeout: int | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension runs the command in the terminal."""
    if not command:
        return {"ok": False, "error": "command is required"}
    params: dict[str, Any] = {"command": command}
    if workdir:
        params["workdir"] = workdir
    if timeout is not None:
        params["timeout"] = timeout
    return deferred("run_command", params)
