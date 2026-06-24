"""query_gitnexus tool — async passthrough to the GitNexus MCP server.

The wrapper exposes a single tool to the LLM and maps its ``query``/``tool``/
``repo`` inputs to the per-operation argument shape the live ``gitnexus`` MCP
server expects. The argument names below are pinned to the live contract
(introspected via ``list_tools`` against ``gitnexus@latest``):

    query          → {"query": <q>}                     required: query
    context        → {"name": <symbol>}                 (uid/file_path optional)
    impact         → {"target": <symbol>, "direction": ...}  required: target, direction
    detect_changes → {"scope": <scope>}                 (analyzes the git diff)
    list_repos     → {}                                 no args

Every non-``list_repos`` operation also takes an optional ``repo`` that the
server makes **required once more than one repository is indexed** — so the
agent should call ``tool="list_repos"`` first and pass the chosen ``repo`` name
on subsequent calls. ``repo`` accepts a bare indexed name (e.g.
``"voyager-interface"``) or group mode ``"@<groupName>"``.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from ..mcp_client import call_mcp_tool, coerce_text

logger = logging.getLogger(__name__)

# Operations exposed by the wrapper, mapped to live GitNexus MCP tools.
_TOOLS = ("query", "context", "impact", "detect_changes", "list_repos")

SCHEMA: Dict[str, Any] = {
    "description": (
        "Query the code-structure index (GitNexus) for symbol definitions, call graphs, "
        "and impact/blast-radius. Call this before answering 'where is X defined', 'what "
        "calls X', or 'what breaks if I change X' — prefer it over guessing about code. "
        "GitNexus is the source of truth for which repos are available: call tool='list_repos' "
        "FIRST to discover indexed repos, then pass the chosen repo name on every other call "
        "(repo is required once more than one repo is indexed). "
        "Example: list_repos -> {repo:'voyager-interface'} then query='TopNav', tool='query', repo='voyager-interface'."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The lookup target — a symbol/function/class name (for query / context / "
                    "impact) or a natural-language keyword query (for query). Required for "
                    "every tool except list_repos and detect_changes."
                ),
            },
            "tool": {
                "type": "string",
                "enum": list(_TOOLS),
                "default": "query",
                "description": (
                    "GitNexus operation: query (find symbols/flows by name or keyword) | "
                    "context (incoming/outgoing references of a symbol) | "
                    "impact (blast radius of changing a symbol) | "
                    "detect_changes (flows affected by the current uncommitted git diff) | "
                    "list_repos (discover indexed repos — no query needed)."
                ),
            },
            "repo": {
                "type": "string",
                "description": (
                    "Indexed repository name from list_repos (e.g. 'voyager-interface'), or "
                    "group mode '@<groupName>'. REQUIRED for query/context/impact/detect_changes "
                    "whenever more than one repo is indexed; omit only when a single repo exists."
                ),
            },
            "direction": {
                "type": "string",
                "enum": ["upstream", "downstream"],
                "default": "upstream",
                "description": (
                    "For tool='impact' only: 'upstream' = what depends on the target (blast "
                    "radius of a change), 'downstream' = what the target depends on."
                ),
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def check_available(**_: Any) -> bool:
    """Return True only when GITNEXUS_MCP_URL is configured."""
    return bool(os.environ.get("GITNEXUS_MCP_URL", "").strip())


def _build_arguments(tool: str, query: str, repo: str, direction: str) -> Dict[str, Any]:
    """Map the wrapper inputs to the live GitNexus MCP per-tool argument shape.

    Argument names are pinned to the live ``gitnexus`` MCP contract (see module
    docstring). ``repo`` is forwarded on every tool except list_repos when set.
    """
    if tool == "list_repos":
        return {}

    args: Dict[str, Any]
    if tool == "context":
        args = {"name": query}
    elif tool == "impact":
        args = {"target": query, "direction": direction or "upstream"}
    elif tool == "detect_changes":
        args = {"scope": "unstaged"}
    else:  # "query"
        args = {"query": query}

    if repo:
        args["repo"] = repo
    return args


async def handle(
    query: Any = "",
    tool: str = "query",
    repo: Any = "",
    direction: str = "upstream",
    **_: Any,
) -> Dict[str, Any]:
    query = coerce_text(query)
    repo = coerce_text(repo)
    if tool not in _TOOLS:
        return {"ok": False, "error": "unknown GitNexus tool %r; expected one of %s." % (tool, ", ".join(_TOOLS))}
    # query/context/impact need a target; detect_changes and list_repos do not.
    if not query and tool not in ("list_repos", "detect_changes"):
        return {"ok": False, "error": "query is required for GitNexus tool %r." % tool}
    url = os.environ.get("GITNEXUS_MCP_URL", "").strip()
    if not url:
        return {"ok": False, "error": "GITNEXUS_MCP_URL is not configured."}
    # Record the context-gathering attempt so the design-write gate is satisfied
    # (see artifacts.py) — marking on attempt, not only on hits.
    from ..context import mark_context_gathered

    mark_context_gathered()
    try:
        results = await call_mcp_tool(url, tool, _build_arguments(tool, query, repo, direction))
        return {"ok": True, "results": results}
    except Exception as exc:
        logger.warning("query_gitnexus failed: %s", exc)
        return {"ok": False, "error": str(exc)}
