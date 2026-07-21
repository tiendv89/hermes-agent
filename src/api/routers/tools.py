"""Tool + skill registry route.

GET /tools — live tool registry + loadable skills (for the FE slash-command picker)
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get("/tools")
async def list_tools_endpoint() -> JSONResponse:
    """Return the live tools + loadable skills for the FE slash-command picker.

    ``tools`` is the plugin registry, honoring each tool's check_fn (gated-off
    tools, where check_fn returns False, are excluded). Each entry's
    ``description`` is the tool's ``short_description`` — a one-line summary
    for the picker UI — falling back to the full SCHEMA description (the
    LLM-facing tool-use instructions) when a tool has no short_description.

    ``skills`` is the bundled skill index — every entry is loadable on demand
    via the ``load_skill`` tool. Each carries a ``type`` of
    ``"technical"`` (knowledge skills) or ``"workflow"`` (workflow skills).
    """
    import plugins

    tools = []
    for t in plugins._TOOLS:
        check_fn = t.get("check_fn")
        if check_fn is not None:
            try:
                if not check_fn():
                    continue
            except Exception:
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
