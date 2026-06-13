"""workflow_load_skill tool — load a skill's full content on demand.

Returns the full SKILL.md body plus any reference files for a named skill.
Only knowledge and authoring skills are loadable; mutation and execution skills
are excluded (they are reimplemented as hermes tools or require a bash/git
environment the gateway does not have).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "name": {
            "type": "string",
            "description": (
                "Name of the skill to load (e.g. 'python-best-practices', "
                "'typescript-best-practices', 'tech-lead', 'init-feature'). "
                "Use the skill index (injected in system context) to find available names."
            ),
        },
    },
    "required": ["name"],
    "additionalProperties": False,
}


def check_available(**_: Any) -> bool:
    """Available when the skill index is configured (WORKFLOW_GITHUB_REPO set)."""
    return bool(os.environ.get("WORKFLOW_GITHUB_REPO", "").strip())


def handle(name: str, **_: Any) -> Dict[str, Any]:
    """Return the full SKILL.md body + reference files for the named skill.

    Returns:
        {ok: True, skill: {name, description, body, references: {filename: content}}}
        or
        {ok: False, error: "..."} on unknown or non-loadable skill name.
    """
    from ..skills import get_skill

    name = name.strip()
    if not name:
        return {"ok": False, "error": "name must be a non-empty string."}

    entry = get_skill(name)
    if entry is None:
        index = __import__("plugins.skills", fromlist=["get_index"]).get_index()
        if not index:
            return {
                "ok": False,
                "error": (
                    "Skill index is empty — WORKFLOW_GITHUB_REPO or GITHUB_TOKEN "
                    "may not be configured."
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
