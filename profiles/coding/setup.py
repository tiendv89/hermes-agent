"""Coding profile — stub (registers no tools, returns empty router).

The coding profile will be populated in subsequent tasks (T3, T4) with
client-executed tools (edit_file, write_file, run_command, git_*, etc.)
that use a deferred-execution model via SSE.

For now, this stub starts without errors, registers zero tools, and returns
an empty APIRouter — enough to verify the profile split infrastructure works.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool registry — empty for now (T3/T4 will populate this).
# ---------------------------------------------------------------------------

_CODING_TOOLS: tuple[dict[str, Any], ...] = ()


# ---------------------------------------------------------------------------
# Profile API
# ---------------------------------------------------------------------------


def register_tools(ctx: Any) -> None:
    """Register coding-profile tools (currently none — stub).

    Delegates to the shared ``plugins.register()`` entry point with an
    empty tool set.
    """
    import plugins

    plugins.register(ctx, tools=_CODING_TOOLS)
    logger.info("coding profile: registered %d tools (stub)", len(_CODING_TOOLS))


def build_router() -> APIRouter:
    """Return an empty APIRouter for the coding profile.

    T4 will add the ``POST /coding/chat`` SSE endpoint here.
    """
    router = APIRouter()
    return router
