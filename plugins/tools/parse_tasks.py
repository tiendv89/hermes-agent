"""parse_tasks tool — parse a feature's tasks.md Index table into task rows.

Parsing is deliberately separated from the workflow-backend HTTP client: this
module owns the tasks.md grammar, and `workflow_backend_client.create_feature_tasks`
only ships an already-parsed list over the wire. The tasks-stage approve
pipeline (and the backup /create-tasks tool) compose the two internally —
parse here, then create via the client.

Index table grammar (go features), strict and positional:

    | ID | Title | Repo | Depends On | Actor |
    |----|-------|------|------------|-------|
    | T1 | Do the thing        | repo-a | —      | agent  |
    | T2 | Do the other thing  | repo-b | T1     | human  |

Each parsed row maps 1:1 onto the workflow-backend `CreateTaskItem` contract:
  - ``name``        ← ID cell (e.g. "T1")   [backend-required field]
  - ``title``       ← Title cell (inline code backticks stripped)
  - ``repo``        ← Repo cell
  - ``depends_on``  ← Depends On cell ("—"/"-"/empty → []; else comma/space-separated T-ids)
  - ``actor_type``  ← Actor cell, normalised to agent|human|either (default agent)
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

# A data row of the Index table — exactly five columns, ID first:
#   | T1 | Title | repo-slug | T2, T3 | agent |
_INDEX_ROW_RE = re.compile(
    r"^\|\s*(T\d+)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|\s*(.*?)\s*\|$"
)

_VALID_ACTORS = frozenset({"agent", "human", "either"})
_DEFAULT_ACTOR = "agent"

# Header cells (lowercased, trimmed) that identify the Index table header row,
# in order. The parser is strict about column layout for go features.
_EXPECTED_HEADERS = ("id", "title", "repo", "depends on", "actor")


def _split_row(stripped: str) -> List[str]:
    """Split a pipe table row into trimmed cell strings.

    ``"| a | b | c |"`` → ``["a", "b", "c"]``.
    """
    inner = stripped.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _is_header_row(cells: List[str]) -> bool:
    """True when the cells match the expected Index header exactly (case-insensitive)."""
    return tuple(c.lower() for c in cells) == _EXPECTED_HEADERS


def _is_separator_row(stripped: str) -> bool:
    """True for a markdown table separator like ``|----|:--:|---|``."""
    return set(stripped) <= set("|-: ")


def parse_tasks_index(tasks_md: str) -> List[Dict[str, Any]]:
    """Parse the Index table from a tasks.md string into task rows.

    Returns a list of dicts keyed by the workflow-backend ``CreateTaskItem``
    contract (``name``, ``title``, ``repo``, ``depends_on``, ``actor_type``).
    Returns an empty list when there is no recognisable Index table — callers
    decide whether "no tasks" is an error.
    """
    tasks: List[Dict[str, Any]] = []
    lines = tasks_md.splitlines()

    header_idx = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if _is_header_row(_split_row(stripped)):
            header_idx = i
            break

    if header_idx == -1:
        return []

    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break  # first non-pipe line after the table ends it
        if _is_separator_row(stripped):
            continue

        m = _INDEX_ROW_RE.match(stripped)
        if not m:
            continue

        task_id, title_raw, repo, depends_raw, actor_raw = (g.strip() for g in m.groups())

        # Strip inline code backticks from the title.
        title = re.sub(r"`([^`]*)`", r"\1", title_raw).strip()

        # "—" / "-" / empty → []; else comma/space-separated T-ids.
        if depends_raw in ("—", "-", ""):
            depends_on: List[str] = []
        else:
            depends_on = [
                d.strip()
                for d in re.split(r"[,\s]+", depends_raw)
                if re.match(r"^T\d+$", d.strip())
            ]

        actor = actor_raw.lower()
        if actor not in _VALID_ACTORS:
            actor = _DEFAULT_ACTOR

        tasks.append(
            {
                "name": task_id,
                "title": title,
                "repo": repo,
                "depends_on": depends_on,
                "actor_type": actor,
            }
        )

    return tasks


SCHEMA: Dict[str, Any] = {
    "description": (
        "Parse a go feature's tasks.md Index table into a structured task list. "
        "Read-only — this does NOT create tasks; it returns exactly the rows that "
        "would be sent to workflow-backend (name, title, repo, depends_on, actor_type).\n\n"
        "Provide tasks_md to parse a string directly, or omit it to read tasks.md "
        "from the current feature. Expected Index columns, in order:\n"
        "  | ID | Title | Repo | Depends On | Actor |\n\n"
        "Use this to preview or validate a task breakdown before approving the "
        "tasks stage."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tasks_md": {
                "type": "string",
                "description": "Raw tasks.md content to parse. Omit to read tasks.md from the current feature.",
            },
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier. Omit to use the current workspace from context.",
            },
            "feature_id": {
                "type": "string",
                "description": "Feature identifier. Omit to use the current feature from context.",
            },
        },
        "required": [],
        "additionalProperties": False,
    },
}


def handle(
    tasks_md: str = "",
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> Dict[str, Any]:
    content = tasks_md or ""

    # No content supplied — resolve and read the current feature's tasks.md.
    if not content.strip():
        from ..context import get_feature_id, get_workspace_id
        from .create_tasks import load_feature_tasks_md

        wid = workspace_id or get_workspace_id()
        fid = feature_id or get_feature_id()
        if not wid or not fid:
            return {
                "ok": False,
                "error": (
                    "Provide tasks_md, or ensure a feature session is active so "
                    "tasks.md can be read from the current feature."
                ),
            }
        github_token = os.environ.get("GITHUB_TOKEN", "").strip()

        loaded = load_feature_tasks_md(wid, fid, github_token)
        if not loaded.get("ok"):
            return {"ok": False, "error": loaded.get("error", "Could not read tasks.md.")}
        content = loaded.get("tasks_md", "")

    tasks = parse_tasks_index(content)
    if not tasks:
        return {
            "ok": False,
            "error": (
                "No tasks parsed. tasks.md must contain an Index table with the "
                "columns: | ID | Title | Repo | Depends On | Actor | and at least "
                "one T<n> row."
            ),
            "tasks": [],
            "count": 0,
        }

    return {"ok": True, "tasks": tasks, "count": len(tasks)}
