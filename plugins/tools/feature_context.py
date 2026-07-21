"""get_feature_context tool — load full feature context on demand.

Calls the same ``get_feature_context()`` function used at session start to
fetch lifecycle state, live task statuses, product spec, and technical design
in a single call. Useful for refreshing context mid-conversation.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA: dict[str, Any] = {
    "description": (
        "Load the current feature's full context on demand — returns lifecycle "
        "stage/status, live task statuses (with blockers and PRs), product spec "
        "summary, and technical design summary. No parameters needed; reads from "
        "the current session's feature scope. Use this to refresh feature context "
        "without restarting the session."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    },
}


def handle(**_: Any) -> dict[str, Any]:
    """Return the current feature's full context block.

    Reads workspace/feature from the session's thread-local context and
    fetches state, tasks, and documents in parallel. Failures are
    non-blocking — missing pieces are noted in the returned block.
    """
    from plugins.feature_context import get_feature_context

    try:
        context_block = get_feature_context()
        if not context_block:
            return {
                "ok": True,
                "context": "No feature context available (no feature_id in session).",
            }
        return {"ok": True, "context": context_block}
    except Exception as exc:
        logger.warning("get_feature_context tool failed: %s", exc)
        return {"ok": False, "error": str(exc)}
