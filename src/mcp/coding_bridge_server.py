"""IDE deferred-tool bridge — an MCP server opencode connects to for coding turns.

The IDE's coding tools (``plugins/tools/``) don't execute anything
server-side — they return a ``{"__deferred__": True, "tool": ..., "params":
...}`` marker that the IDE extension picks up over SSE
(``hermes.tool.deferred``) and executes on the developer's own machine.

opencode needs to call these SAME operations itself — but opencode's own
agent loop runs against MCP tools, not this gateway's internal tool
registry. This module exposes one MCP server per coding session (built by
``build_bridge_app``) whose tools:

1. Reuse the EXISTING schemas/handlers in ``plugins/tools/`` for
   parameter validation and deferred-marker construction — no duplicated
   business logic, no second source of truth for what these tools accept.
2. Push a ``hermes.tool.deferred`` event onto the session's live SSE
   translator (looked up via
   ``src.services.deferred_tool_gateway.get_translator`` — registered by
   the opencode turn runner before the turn starts), so the IDE extension
   sees an identical event to the Hermes-driven path.
3. Block (off the event loop, via
   ``deferred_tool_gateway.await_response``, which runs on a dedicated
   thread pool) until the IDE reports the real result via
   ``POST /coding/sessions/{session_id}/tool-result``, then return that
   result to opencode.

Mounted per-session (see ``src/app.py``) since each session needs its own
translator/session_id closure — one shared MCP app can't disambiguate which
in-flight coding session a given tool call belongs to.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)


async def _defer_and_wait(session_id: str, tool: str, params: dict[str, Any]) -> dict[str, Any]:
    """Publish a deferred-tool event to the IDE and block for its real result."""
    from src.services import deferred_tool_gateway as gw

    call_id = uuid.uuid4().hex
    gw.register(call_id, session_id, tool, params)

    translator = gw.get_translator(session_id)
    if translator is not None:
        translator.on_tool_start(call_id=call_id, name=tool, args=params)
        translator.on_tool_complete(
            call_id=call_id,
            name=tool,
            args=params,
            output={"__deferred__": True, "tool": tool, "params": params},
        )
    else:
        logger.warning(
            "coding_bridge: no live translator registered for session %s — "
            "the IDE extension will never see this %s call",
            session_id,
            tool,
        )

    timeout = gw.get_deferred_tool_timeout()
    result = await gw.await_response(call_id, timeout)
    if result is None:
        return {
            "ok": False,
            "error": f"timed out after {timeout}s waiting for the IDE to respond",
        }
    return result


def build_bridge_app(session_id: str):
    """Return a fresh ``FastMCP`` instance whose tools are bound to *session_id*.

    Each tool mirrors one of ``plugins/tools/``'s existing coding
    tools (same parameters, same validation) but defers to the IDE via
    ``_defer_and_wait`` instead of returning the marker for Hermes's own
    conversation loop to surface.
    """
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = FastMCP(
        f"hermes-ide-bridge-{session_id}",
        # DNS-rebinding protection guards against a malicious webpage's JS
        # reaching an internal service via attacker-controlled DNS — not a
        # relevant threat model here, since the only caller is opencode's
        # own backend process (server-to-server), never a browser. This
        # endpoint's actual security boundary is the gateway's own network
        # perimeter plus the unguessable session_id path segment, same as
        # every other internal-only route in this service.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    async def _run(tool: str, params: dict[str, Any]) -> dict[str, Any]:
        return await _defer_and_wait(session_id, tool, params)

    # ── File operations ──────────────────────────────────────────────

    @mcp.tool()
    async def read_file(
        path: str, start_line: int | None = None, end_line: int | None = None
    ) -> dict:
        """Read a file's content from the IDE workspace, optionally restricted
        to a 1-indexed, inclusive line range."""
        from plugins.tools.local_file_ops import handle_read_file

        marker = handle_read_file(path=path, start_line=start_line, end_line=end_line)
        if not marker.get("__deferred__"):
            return marker
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def edit_file(path: str, edits: list[dict[str, str]]) -> dict:
        """Apply an ordered list of find-and-replace edits to a file via the
        IDE's native editor API. Each edit is {old_string, new_string}."""
        from plugins.tools.local_file_ops import handle_edit_file

        marker = handle_edit_file(path=path, edits=edits)
        if not marker.get("__deferred__"):
            return marker
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def write_file(path: str, content: str) -> dict:
        """Create or overwrite a file in the IDE workspace."""
        from plugins.tools.local_file_ops import handle_write_file

        marker = handle_write_file(path=path, content=content)
        if not marker.get("__deferred__"):
            return marker
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def create_directory(path: str) -> dict:
        """Create a directory (and any missing parents) in the IDE workspace."""
        from plugins.tools.local_file_ops import handle_create_directory

        marker = handle_create_directory(path=path)
        if not marker.get("__deferred__"):
            return marker
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def browse_directory(path: str = "") -> dict:
        """List files and subdirectories in a directory (defaults to the
        workspace root)."""
        from plugins.tools.local_file_ops import handle_browse_directory

        marker = handle_browse_directory(path=path)
        if not marker.get("__deferred__"):
            return marker
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def search_code(pattern: str, path: str = "", file_glob: str = "") -> dict:
        """Search file contents for a regex pattern (grep/ripgrep-style)."""
        from plugins.tools.local_file_ops import handle_search_code

        marker = handle_search_code(pattern=pattern, path=path, file_glob=file_glob)
        if not marker.get("__deferred__"):
            return marker
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def search_files(pattern: str, path: str = "") -> dict:
        """Find files by glob pattern in the IDE workspace."""
        from plugins.tools.local_file_ops import handle_search_files

        marker = handle_search_files(pattern=pattern, path=path)
        if not marker.get("__deferred__"):
            return marker
        return await _run(marker["tool"], marker["params"])

    # ── Terminal ──────────────────────────────────────────────────────

    @mcp.tool()
    async def run_command(
        command: str, workdir: str = "", timeout: int | None = None
    ) -> dict:
        """Run a shell command in the developer's IDE terminal. Returns
        stdout, stderr, and exit code."""
        from plugins.tools.terminal import handle_run_command

        marker = handle_run_command(command=command, workdir=workdir, timeout=timeout)
        if not marker.get("__deferred__"):
            return marker
        return await _run(marker["tool"], marker["params"])

    # ── Git operations ────────────────────────────────────────────────

    @mcp.tool()
    async def git_status() -> dict:
        """Get the working-tree status from the developer's local git repo."""
        from plugins.tools.git_ops import handle_git_status

        marker = handle_git_status()
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def git_diff(staged: bool | None = None, path: str = "") -> dict:
        """Get the unified diff of uncommitted changes."""
        from plugins.tools.git_ops import handle_git_diff

        marker = handle_git_diff(staged=staged, path=path)
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def git_commit(message: str) -> dict:
        """Commit staged changes with a message."""
        from plugins.tools.git_ops import handle_git_commit

        marker = handle_git_commit(message=message)
        if not marker.get("__deferred__"):
            return marker
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def git_push(
        remote: str = "", branch: str = "", set_upstream: bool = False
    ) -> dict:
        """Push commits to the remote."""
        from plugins.tools.git_ops import handle_git_push

        marker = handle_git_push(remote=remote, branch=branch, set_upstream=set_upstream)
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def git_checkout(branch: str, create: bool = False) -> dict:
        """Switch to a branch (or create a new one)."""
        from plugins.tools.git_ops import handle_git_checkout

        marker = handle_git_checkout(branch=branch, create=create)
        if not marker.get("__deferred__"):
            return marker
        return await _run(marker["tool"], marker["params"])

    @mcp.tool()
    async def git_log(count: int | None = None, branch: str = "") -> dict:
        """Show recent commit history."""
        from plugins.tools.git_ops import handle_git_log

        marker = handle_git_log(count=count, branch=branch)
        return await _run(marker["tool"], marker["params"])

    return mcp
