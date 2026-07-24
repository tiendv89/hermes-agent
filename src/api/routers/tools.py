"""Tool + skill registry route.

GET /tools — live tool registry + loadable skills (for the FE slash-command picker)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/tools")
async def list_tools_endpoint(
    source: str = Query("", description="Pass 'coding-ide' for the IDE's own toolset; omit for the web workflow chat's."),
) -> JSONResponse:
    """Return the live tools + loadable skills for the FE slash-command picker.

    ``tools`` reflects one of two fixed toolsets, matching whichever process
    actually executes a turn for that surface (see src/tool_setup.py's
    register_tools — _WORKFLOW_TOOLS for the web chat, _CODING_TOOLS for the
    IDE extension). These aren't read from plugins._TOOLS/the live
    ToolRegistry: both profiles' register() calls run once at startup in this
    single merged process, and each overwrites plugins._TOOLS with whatever
    it JUST registered — so that global only ever reflects whichever
    profile's register() call happened to run last, regardless of which
    surface is actually asking. Reading directly from the two source tuples
    sidesteps that entirely and is always correct for the caller.

    Each entry's ``description`` is the tool's ``short_description`` — a
    one-line summary for the picker UI — falling back to the full SCHEMA
    description (the LLM-facing tool-use instructions) when a tool has no
    short_description. check_fn-gated tools (check_fn() returns False) are
    excluded, same as before.

    ``skills`` is the bundled skill index — every entry is loadable on demand
    via the ``load_skill`` tool. Each carries a ``type`` of
    ``"technical"`` (knowledge skills) or ``"workflow"`` (workflow skills).
    """
    from src.tool_setup import _CODING_TOOLS, _WORKFLOW_TOOLS

    tool_defs = _CODING_TOOLS if source == "coding-ide" else _WORKFLOW_TOOLS

    tools = []
    for t in tool_defs:
        check_fn = t.get("check_fn")
        if check_fn is not None:
            try:
                if not check_fn():
                    continue
            except Exception:
                logger.debug("check_fn raised for tool %r; treating as unavailable", t["name"], exc_info=True)
                continue
        tools.append(
            {
                "name": t["name"],
                "description": t.get("short_description")
                or t.get("schema", {}).get("description", ""),
            }
        )

    skills = []
    try:
        from plugins.skills import get_index

        for entry in sorted(get_index().values(), key=lambda e: e.name):
            skills.append(
                {
                    "name": entry.name,
                    "description": entry.description,
                    "type": "workflow" if entry.is_authoring else "technical",
                }
            )
    except Exception:
        logger.exception("list_tools: failed to build the skill list")

    return JSONResponse({"tools": tools, "skills": skills})
