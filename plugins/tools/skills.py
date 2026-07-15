"""load_skill tool — load a skill's full content on demand.

Returns the full SKILL.md body plus any reference files for a named skill.
Only knowledge and authoring skills are loadable; mutation and execution skills
are excluded (they are reimplemented as hermes tools or require a bash/git
environment the gateway does not have).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "description": (
        "Load a skill's full guidance on demand — returns the named skill's "
        "SKILL.md body plus any reference files. Use when you need detailed "
        "best-practices; pick a name from the skill index injected in context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Name of the skill to load (e.g. 'python-best-practices', "
                    "'typescript-best-practices'). "
                    "Use the skill index (injected in system context) to find available names."
                ),
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


def check_available(**_: Any) -> bool:
    """Available whenever the bundled skill index has at least one skill."""
    from ..skills import get_index

    return bool(get_index())


def _coerce_name(name: Any) -> str:
    """Normalize the ``name`` argument to a plain string.

    Over the MCP path the model frequently passes a structured object instead
    of a bare string (e.g. ``{"name": "tech-lead"}``), which would otherwise
    blow up on ``name.strip()`` with ``'dict' object has no attribute 'strip'``.
    """
    if isinstance(name, dict):
        name = name.get("name") or name.get("skill") or ""
    if not isinstance(name, str):
        name = "" if name is None else str(name)
    return name.strip()


def handle(name: Any = "", **_: Any) -> Dict[str, Any]:
    """Return the full SKILL.md body + reference files for the named skill.

    Returns:
        {ok: True, skill: {name, description, body, references: {filename: content}}}
        or
        {ok: False, error: "..."} on unknown or non-loadable skill name.
    """
    from ..skills import get_skill

    name = _coerce_name(name)
    if not name:
        return {"ok": False, "error": "name must be a non-empty string."}

    entry = get_skill(name)
    if entry is None:
        index = __import__("plugins.skills", fromlist=["get_index"]).get_index()
        if not index:
            return {
                "ok": False,
                "error": (
                    "Skill index is empty — the bundled skills directory "
                    "(plugins/skills) is missing or unreadable."
                ),
            }
        available = sorted(index.keys())
        return {
            "ok": False,
            "error": (
                f"Unknown or non-loadable skill {name!r}. "
                f"Available skills: {', '.join(available)}."
            ),
        }

    return {
        "ok": True,
        "skill": {
            "name": entry.name,
            "description": entry.description,
            "body": entry.body,
            "references": dict(entry.references),
        },
    }
