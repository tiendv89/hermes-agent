"""parse_tasks tool — parse a feature's tasks.md Index table into task rows.

Parsing is deliberately separated from the workflow-backend HTTP client: this
module owns the tasks.md grammar, and `workflow_backend_client.create_feature_tasks`
only ships an already-parsed list over the wire. The tasks-stage approve
pipeline (and the backup /create-tasks tool) compose the two internally —
parse here, then create via the client.

Index table grammar (go features) — header-driven, order-flexible:

    | ID | Title | Repo | Depends On | Actor | Model |
    |----|-------|------|------------|-------|-------|
    | T1 | Do the thing        | repo-a | —      | agent  | Claude Sonnet 4.6 |
    | T2 | Do the other thing  | repo-b | T1     | human  |                   |

The parser reads the header row to learn each column's position, then pulls
cells by name — so column order is flexible, unknown/extra columns are ignored,
and optional columns may simply be absent. A table is recognised as the Index
table when its header contains every *required* column (``id``, ``title``,
``repo``, ``depends on``, ``actor``); ``model`` is optional.

Each parsed row maps 1:1 onto the workflow-backend `CreateTaskItem` contract:
  - ``name``        ← ID cell (e.g. "T1")   [backend-required field]
  - ``title``       ← Title cell (inline code backticks stripped)
  - ``repo``        ← Repo cell
  - ``depends_on``  ← Depends On cell ("—"/"-"/empty → []; else comma/space-separated ids)
  - ``actor_type``  ← Actor cell, normalised to agent|human|either (default agent)
  - ``model``       ← Model cell, display name for agent-actor tasks; "" for
                      human/either tasks and for tables without a Model column

Backward compatibility (configure-executor-types-model-policies): the ``Model``
column is optional, so pre-existing five-column tasks.md files (no Model column)
still parse — every row simply gets ``model == ""``. The ``T<n>`` id shape is a
convention only; any non-empty id cell is accepted, so other id formats parse
without a parser change.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_VALID_ACTORS = frozenset({"agent", "human", "either"})
_DEFAULT_ACTOR = "agent"

# Column names (lowercased, trimmed) that must all be present for a pipe row to
# be recognised as the Index table header. ``model`` is optional; any other
# columns are ignored.
_REQUIRED_HEADERS = frozenset({"id", "title", "repo", "depends on", "actor"})


def _split_row(stripped: str) -> list[str]:
    """Split a pipe table row into trimmed cell strings.

    ``"| a | b | c |"`` → ``["a", "b", "c"]``.
    """
    inner = stripped.strip()
    inner = inner.removeprefix("|")
    inner = inner.removesuffix("|")
    return [c.strip() for c in inner.split("|")]


def _header_index(cells: list[str]) -> dict[str, int] | None:
    """Return a ``{column_name: position}`` map when the cells are an Index header.

    A row qualifies as the header when its lowercased column names cover every
    entry in ``_REQUIRED_HEADERS`` (case-insensitive, order-independent). Returns
    ``None`` otherwise. Unknown columns are kept in the map but ignored by the
    row parser.
    """
    col_index = {c.lower(): i for i, c in enumerate(cells)}
    if _REQUIRED_HEADERS <= col_index.keys():
        return col_index
    return None


def _cell(cells: list[str], col_index: dict[str, int], name: str) -> str:
    """Read a column's cell value by name, or ``""`` if absent / out of range.

    Absent when the header has no such column (e.g. ``model`` in a legacy table);
    out of range when a data row has fewer cells than the header.
    """
    idx = col_index.get(name)
    if idx is None or idx >= len(cells):
        return ""
    return cells[idx].strip()


def _is_separator_row(stripped: str) -> bool:
    """True for a markdown table separator like ``|----|:--:|---|``."""
    return set(stripped) <= set("|-: ")


def parse_tasks_index(tasks_md: str) -> list[dict[str, Any]]:
    """Parse the Index table from a tasks.md string into task rows.

    Returns a list of dicts keyed by the workflow-backend ``CreateTaskItem``
    contract (``name``, ``title``, ``repo``, ``depends_on``, ``actor_type``).
    Returns an empty list when there is no recognisable Index table — callers
    decide whether "no tasks" is an error.
    """
    tasks: list[dict[str, Any]] = []
    lines = tasks_md.splitlines()

    header_idx = -1
    col_index: dict[str, int] = {}
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        found = _header_index(_split_row(stripped))
        if found is not None:
            header_idx = i
            col_index = found
            break

    if header_idx == -1:
        return []

    for line in lines[header_idx + 1:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break  # first non-pipe line after the table ends it
        if _is_separator_row(stripped):
            continue

        cells = _split_row(stripped)

        # A data row is any non-separator pipe row with a non-empty ID cell.
        # The "T<n>" shape is a convention, not a rule — do not enforce it.
        task_id = _cell(cells, col_index, "id")
        if not task_id:
            continue

        title_raw = _cell(cells, col_index, "title")
        repo = _cell(cells, col_index, "repo")
        depends_raw = _cell(cells, col_index, "depends on")
        actor_raw = _cell(cells, col_index, "actor")
        model_raw = _cell(cells, col_index, "model")

        # Strip inline code backticks from the title.
        title = re.sub(r"`([^`]*)`", r"\1", title_raw).strip()

        # "—" / "-" / empty → []; else comma/space-separated ids (kept verbatim,
        # since the id shape is not enforced).
        if depends_raw in ("—", "-", ""):
            depends_on: list[str] = []
        else:
            depends_on = [d for d in re.split(r"[,\s]+", depends_raw) if d]

        actor = actor_raw.lower()
        if actor not in _VALID_ACTORS:
            actor = _DEFAULT_ACTOR

        # model: display name for agent tasks, empty for human/either and for
        # tables without a Model column. "—"/"-" normalised to "".
        model = model_raw if model_raw not in ("—", "-") else ""

        tasks.append(
            {
                "name": task_id,
                "title": title,
                "repo": repo,
                "depends_on": depends_on,
                "actor_type": actor,
                "model": model,
            }
        )

    return tasks


SCHEMA: dict[str, Any] = {
    "description": (
        "Parse a go feature's tasks.md Index table into a structured task list. "
        "Read-only — this does NOT create tasks; it returns exactly the rows that "
        "would be sent to workflow-backend "
        "(name, title, repo, depends_on, actor_type, model).\n\n"
        "Provide tasks_md to parse a string directly, or omit it to read tasks.md "
        "from the current feature. Required Index columns (any order):\n"
        "  | ID | Title | Repo | Depends On | Actor |\n\n"
        "The Model column is optional; extra columns are ignored. Model holds the "
        "implementation-phase model display name for agent-actor tasks and is blank "
        "for human/either tasks and for tables without a Model column.\n\n"
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
) -> dict[str, Any]:
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
        loaded = load_feature_tasks_md(wid, fid)
        if not loaded.get("ok"):
            return {"ok": False, "error": loaded.get("error", "Could not read tasks.md.")}
        content = loaded.get("tasks_md", "")

    tasks = parse_tasks_index(content)
    if not tasks:
        return {
            "ok": False,
            "error": (
                "No tasks parsed. tasks.md must contain an Index table whose header "
                "includes the columns | ID | Title | Repo | Depends On | Actor | "
                "(in any order; a Model column is optional) and at least one data "
                "row with a non-empty ID cell."
            ),
            "tasks": [],
            "count": 0,
        }

    return {"ok": True, "tasks": tasks, "count": len(tasks)}
