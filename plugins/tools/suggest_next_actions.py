"""suggest_next_actions — local executor tool for CTA suggestions.

The Hermes agent calls this tool at the end of a turn to surface 1–3
context-aware next actions. The handler:
  1. Validates the suggestions payload against the CtaSuggestion schema.
  2. Persists the suggestions to messages.cta_suggestions for the current
     assistant message.
  3. Publishes a turn.cta_suggestions event on the in-process SSE bus.
  4. Returns {"status": "ok"} so the agent turn ends cleanly.

This is a local handler — no external API call is made.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_VALID_CATEGORIES = frozenset(
    ["Lifecycle", "Clarify", "Review", "Edit", "Action", "GitNexus", "RAG"]
)

TOOL_SCHEMA: Dict[str, Any] = {
    "name": "suggest_next_actions",
    "description": (
        "Suggest 1–3 context-aware next actions the user could take. "
        "Call this at the end of a turn when a natural follow-up exists. "
        "Omit the call when no clear next step applies."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "suggestions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "id",
                        "title",
                        "category",
                        "description",
                        "action_text",
                        "button_label",
                    ],
                    "properties": {
                        "id": {"type": "string"},
                        "title": {"type": "string", "maxLength": 40},
                        "category": {
                            "type": "string",
                            "enum": list(_VALID_CATEGORIES),
                        },
                        "description": {"type": "string", "maxLength": 120},
                        "action_text": {"type": "string"},
                        "button_label": {"type": "string", "maxLength": 20},
                        "icon": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                "minItems": 1,
                "maxItems": 3,
            }
        },
        "required": ["suggestions"],
        "additionalProperties": False,
    },
}

# Canonical schema for plugin registry registration (parameters-only form).
SCHEMA: Dict[str, Any] = {
    "description": TOOL_SCHEMA["description"],
    "parameters": TOOL_SCHEMA["input_schema"],
}


def _validate_suggestions(suggestions: Any) -> List[Dict[str, Any]]:
    """Validate the suggestions list; raise ValueError with a descriptive message on failure."""
    if not isinstance(suggestions, list):
        raise ValueError("suggestions must be an array")
    if not suggestions:
        raise ValueError("suggestions must contain at least 1 item")
    if len(suggestions) > 3:
        raise ValueError(
            f"suggestions must contain at most 3 items, got {len(suggestions)}"
        )
    required_fields = {"id", "title", "category", "description", "action_text", "button_label"}
    for i, s in enumerate(suggestions):
        if not isinstance(s, dict):
            raise ValueError(f"suggestions[{i}] must be an object")
        missing = required_fields - s.keys()
        if missing:
            raise ValueError(
                f"suggestions[{i}] is missing required fields: {sorted(missing)}"
            )
        category = s.get("category")
        if category not in _VALID_CATEGORIES:
            raise ValueError(
                f"suggestions[{i}].category {category!r} is not one of "
                f"{sorted(_VALID_CATEGORIES)}"
            )
        title = s.get("title", "")
        if len(title) > 40:
            raise ValueError(
                f"suggestions[{i}].title exceeds 40 characters ({len(title)})"
            )
        desc = s.get("description", "")
        if len(desc) > 120:
            raise ValueError(
                f"suggestions[{i}].description exceeds 120 characters ({len(desc)})"
            )
        label = s.get("button_label", "")
        if len(label) > 20:
            raise ValueError(
                f"suggestions[{i}].button_label exceeds 20 characters ({len(label)})"
            )
    return suggestions


def handle(suggestions: Any = None, **_: Any) -> Dict[str, Any]:
    """Local executor handler for suggest_next_actions.

    Validates, persists to messages.cta_suggestions, and publishes the
    turn.cta_suggestions SSE event. Returns {"status": "ok"} on success.
    """
    from plugins.context import (
        get_agent_db_factory,
        get_agent_loop,
        get_agent_session_id,
    )

    # Validate payload.
    try:
        validated = _validate_suggestions(suggestions)
    except ValueError as exc:
        logger.warning("suggest_next_actions: validation failed: %s", exc)
        return {"status": "error", "error": str(exc)}

    session_id = get_agent_session_id()
    loop = get_agent_loop()
    db_factory = get_agent_db_factory()

    if not session_id:
        logger.warning(
            "suggest_next_actions: no session_id in context — skipping persist and publish"
        )
        return {"status": "ok", "warning": "no session context, suggestions not persisted"}

    # Persist to messages.cta_suggestions and publish SSE event.
    if loop is not None and db_factory is not None:
        future = asyncio.run_coroutine_threadsafe(
            _persist_and_publish(session_id, validated, db_factory, loop),
            loop,
        )
        try:
            future.result(timeout=15)
        except Exception:
            logger.exception(
                "suggest_next_actions: failed to persist/publish for session %s",
                session_id,
            )
            return {"status": "error", "error": "failed to persist or publish suggestions"}
    else:
        logger.warning(
            "suggest_next_actions: loop or db_factory unavailable — "
            "publishing SSE event only (no DB persist)"
        )
        _publish_bus(session_id, None, validated, loop)

    return {"status": "ok"}


async def _persist_and_publish(
    session_id: str,
    suggestions: List[Dict[str, Any]],
    db_factory: Any,
    loop: Any,
) -> None:
    """Persist suggestions to the DB and publish the SSE event."""
    from src.db.store import get_latest_assistant_message_id, update_message_cta_suggestions

    message_id: int | None = None
    async with db_factory() as db:
        message_id = await get_latest_assistant_message_id(db, session_id)
        if message_id is not None:
            await update_message_cta_suggestions(db, session_id, message_id, suggestions)

    _publish_bus(session_id, message_id, suggestions, loop)


def _publish_bus(
    session_id: str,
    message_id: int | None,
    suggestions: List[Dict[str, Any]],
    loop: Any,
) -> None:
    """Publish the turn.cta_suggestions event on the SSE bus."""
    from src.realtime.bus import get_bus

    event: Dict[str, Any] = {
        "event": "turn.cta_suggestions",
        "data": {
            "message_id": message_id,
            "suggestions": suggestions,
        },
    }
    bus = get_bus()
    if loop is not None:
        loop.call_soon_threadsafe(bus.publish, session_id, event)
    else:
        # Fallback: call directly (only safe from the event loop thread).
        bus.publish(session_id, event)
