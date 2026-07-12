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

import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from ..mcp_client import call_mcp_tool, coerce_text

logger = logging.getLogger(__name__)

_REPO_CACHE_TTL = float(os.environ.get("GITNEXUS_REPO_CACHE_TTL", "600"))
# Keyed by workspace_id (workflow-backend's workspaces.id, a UUID — no
# slug resolution; matches rag-service's convention).
_repo_cache: Dict[str, Dict[str, Any]] = {}

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


def _build_arguments(
    tool: str, query: str, repo: str, direction: str
) -> Dict[str, Any]:
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
        return {
            "ok": False,
            "error": "unknown GitNexus tool %r; expected one of %s."
            % (tool, ", ".join(_TOOLS)),
        }
    # query/context/impact need a target; detect_changes and list_repos do not.
    if not query and tool not in ("list_repos", "detect_changes"):
        return {"ok": False, "error": "query is required for GitNexus tool %r." % tool}
    url = os.environ.get("GITNEXUS_MCP_URL", "").strip()
    if not url:
        return {"ok": False, "error": "GITNEXUS_MCP_URL is not configured."}
    # Record the context-gathering attempt so the design-write gate is satisfied
    # (see artifacts.py) — marking on attempt, not only on hits.
    from ..context import get_org_id, get_workspace_id, mark_context_gathered

    mark_context_gathered()
    workspace_id = get_workspace_id()
    if not workspace_id:
        return {
            "ok": False,
            "error": "workspace_id is required but was not provided and no workspace context is set.",
        }
    # GitNexus's MCP endpoint is now org+workspace scoped
    # (/ws/<organization_id>/<workspace_id>/sse, matching rag-service), so
    # organization_id is required just like workspace_id above.
    org_id = get_org_id()
    if not org_id:
        return {
            "ok": False,
            "error": "organization_id is required but no organization context is set for this session.",
        }
    try:
        results = await call_mcp_tool(
            url,
            tool,
            _build_arguments(tool, query, repo, direction),
            workspace_id=workspace_id,
            organization_id=org_id,
        )
        return {"ok": True, "results": results}
    except Exception as exc:
        logger.warning("query_gitnexus failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def _loads_leading_json(text: str) -> Any:
    """Decode the leading JSON value in *text*, ignoring any trailing prose.

    GitNexus's list_repos returns a pretty-printed JSON array followed by a
    human-readable footer (e.g. "READ gitnexus://repo/{name}/context ..."), so a
    plain json.loads fails with "Extra data". raw_decode parses just the first
    value.
    """
    s = text.lstrip()
    start = next((i for i, ch in enumerate(s) if ch in "[{"), -1)
    if start < 0:
        return None
    try:
        obj, _idx = json.JSONDecoder().raw_decode(s[start:])
        return obj
    except Exception:
        return None


def _parse_repo_names(results: Any) -> List[str]:
    """Extract repo names from a list_repos MCP result (list of content dicts)."""
    names: List[str] = []
    for item in results or []:
        text = item.get("text") if isinstance(item, dict) else None
        if not text:
            continue
        data = _loads_leading_json(text)
        if isinstance(data, list):
            for r in data:
                if isinstance(r, dict) and r.get("name"):
                    names.append(str(r["name"]))
    return names


def _run_coro_sync(coro: Any) -> Any:
    """Run an async coroutine from sync code, whether or not a loop is running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # A loop is already running in this thread — run the coroutine in a separate
    # thread with its own loop so we don't deadlock the existing one.
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(coro)).result()


def list_indexed_repos(
    use_cache: bool = True,
    timeout: Optional[float] = None,
    workspace_id: str = "",
    organization_id: str = "",
) -> Optional[List[str]]:
    """Best-effort, synchronous list of GitNexus-indexed repo names.

    Returns the repo names (e.g. ['voyager-interface', ...]), or ``None`` when
    GitNexus is unconfigured/unreachable so callers can fall back gracefully.

    The request targets the org+workspace-scoped endpoint
    (``…/ws/<organization_id>/<workspace_id>/sse``, matching rag-service) so
    only that workspace's repos are returned. Both *workspace_id* and
    *organization_id* default to the current session context when omitted —
    no slug resolution, GitNexus is keyed by the raw workspace_id UUID same
    as organization_id. Results are cached per workspace_id (already
    globally unique) for ``_REPO_CACHE_TTL`` seconds. A missing workspace_id
    or organization_id returns ``None`` immediately since GitNexus only
    serves scoped endpoints.
    """
    url = os.environ.get("GITNEXUS_MCP_URL", "").strip()
    if not url:
        return None
    from ..context import get_org_id, get_workspace_id

    workspace_id = workspace_id or get_workspace_id()
    if not workspace_id:
        logger.debug(
            "list_indexed_repos: no workspace_id in context — GitNexus requires "
            "a workspace-scoped endpoint, skipping"
        )
        return None
    organization_id = organization_id or get_org_id()
    if not organization_id:
        logger.debug(
            "list_indexed_repos: no organization_id in context — GitNexus requires "
            "an org-scoped endpoint, skipping"
        )
        return None
    now = time.time()
    cache = _repo_cache.setdefault(workspace_id, {"names": None, "ts": 0.0})
    if (
        use_cache
        and cache["names"] is not None
        and (now - cache["ts"]) < _REPO_CACHE_TTL
    ):
        return cache["names"]

    async def _go() -> Any:
        coro = call_mcp_tool(
            url, "list_repos", {}, workspace_id=workspace_id, organization_id=organization_id
        )
        if timeout:
            return await asyncio.wait_for(coro, timeout)
        return await coro

    try:
        names = _parse_repo_names(_run_coro_sync(_go()))
    except Exception as exc:
        logger.debug("list_indexed_repos failed: %s", exc)
        return None
    if not names:
        # Treat an empty/unparseable result as "unavailable" — don't cache it,
        # so a transient miss doesn't blind callers for the whole TTL window.
        return None
    cache["names"] = names
    cache["ts"] = now
    return names
