"""Tests for the parse_tasks tool — header-driven tasks.md Index-table parsing.

Grammar under test (go features) — required columns, order-flexible:

    | ID | Title | Repo | Depends On | Actor |   (Model optional; extras ignored)

Parsed rows map onto the workflow-backend CreateTaskItem contract:
    name, title, repo, depends_on, actor_type, model.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _load_parse_tasks():
    """Load plugins.tools.parse_tasks without importing the heavy plugins package."""
    if "plugins.tools.parse_tasks" in sys.modules:
        return sys.modules["plugins.tools.parse_tasks"]

    import types

    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(REPO_ROOT / "plugins")]
        pkg.__package__ = "plugins"
        sys.modules["plugins"] = pkg
    if "plugins.tools" not in sys.modules:
        tools_pkg = types.ModuleType("plugins.tools")
        tools_pkg.__path__ = [str(REPO_ROOT / "plugins" / "tools")]
        tools_pkg.__package__ = "plugins.tools"
        sys.modules["plugins.tools"] = tools_pkg

    path = REPO_ROOT / "plugins" / "tools" / "parse_tasks.py"
    spec = importlib.util.spec_from_file_location("plugins.tools.parse_tasks", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plugins.tools.parse_tasks"] = mod
    spec.loader.exec_module(mod)
    return mod


_MOD = _load_parse_tasks()
parse = _MOD.parse_tasks_index

# Six-column sample (configure-executor-types-model-policies grammar).
_SAMPLE = """\
# Tasks — demo

## Index

| ID | Title | Repo | Depends On | Actor | Model |
|----|-------|------|------------|-------|-------|
| T1 | Thread `user_id` into context | hermes-agent | — | agent | Claude Sonnet 4.6 |
| T2 | Stop at `tasks.md` | hermes-agent | — | human |  |
| T3 | Server-side guard | workflow-backend | — | either |  |
| T4 | Service client | hermes-agent | T1 | agent | Claude Opus 4.8 |
| T5 | Approve orchestration | hermes-agent | T2, T3, T4 | agent | Claude Haiku 4.5 |

## T1 — Thread user_id
"""

# Required-columns-only sample (no optional Model column) — parses with
# model == "" on every row.
_SAMPLE_5COL = """\
# Tasks — demo

## Index

| ID | Title | Repo | Depends On | Actor |
|----|-------|------|------------|-------|
| T1 | Thread `user_id` into context | hermes-agent | — | agent |
| T2 | Stop at `tasks.md` | hermes-agent | — | human |
"""


class TestParseTasksIndex:
    def test_parses_all_rows(self):
        assert len(parse(_SAMPLE)) == 5

    def test_names_are_task_ids(self):
        assert [t["name"] for t in parse(_SAMPLE)] == ["T1", "T2", "T3", "T4", "T5"]

    def test_repo_populated(self):
        tasks = parse(_SAMPLE)
        assert tasks[0]["repo"] == "hermes-agent"
        assert tasks[2]["repo"] == "workflow-backend"

    def test_no_depends_is_empty_list(self):
        tasks = parse(_SAMPLE)
        assert tasks[0]["depends_on"] == []
        assert tasks[1]["depends_on"] == []

    def test_single_depends(self):
        t4 = next(t for t in parse(_SAMPLE) if t["name"] == "T4")
        assert t4["depends_on"] == ["T1"]

    def test_multi_depends(self):
        t5 = next(t for t in parse(_SAMPLE) if t["name"] == "T5")
        assert t5["depends_on"] == ["T2", "T3", "T4"]

    def test_backticks_stripped_from_title(self):
        tasks = parse(_SAMPLE)
        assert "`" not in tasks[0]["title"]
        assert "user_id" in tasks[0]["title"]

    def test_actor_from_column(self):
        tasks = parse(_SAMPLE)
        assert tasks[0]["actor_type"] == "agent"
        assert tasks[1]["actor_type"] == "human"
        assert tasks[2]["actor_type"] == "either"

    def test_invalid_actor_defaults_to_agent(self):
        md = (
            "## Index\n\n"
            "| ID | Title | Repo | Depends On | Actor | Model |\n"
            "|----|-------|------|------------|-------|-------|\n"
            "| T1 | Task | my-repo | — | robot |  |\n"
        )
        assert parse(md)[0]["actor_type"] == "agent"

    def test_blank_actor_defaults_to_agent(self):
        md = (
            "## Index\n\n"
            "| ID | Title | Repo | Depends On | Actor | Model |\n"
            "|----|-------|------|------------|-------|-------|\n"
            "| T1 | Task | my-repo | — |  |  |\n"
        )
        assert parse(md)[0]["actor_type"] == "agent"

    def test_empty_string_returns_empty(self):
        assert parse("") == []

    def test_no_index_table_returns_empty(self):
        assert parse("# Tasks\n\nJust prose, no table.\n") == []

    def test_wrong_columns_return_empty(self):
        """The old Wave format must NOT parse under the strict grammar."""
        md = (
            "## Index\n\n"
            "| ID | Wave | Title | Repo | Depends on |\n"
            "|----|------|-------|------|------------|\n"
            "| T1 | 1 | Task | my-repo | — |\n"
        )
        assert parse(md) == []

    def test_header_case_insensitive(self):
        md = (
            "## Index\n\n"
            "| id | title | repo | depends on | actor | model |\n"
            "|----|-------|------|------------|-------|-------|\n"
            "| T1 | Task | my-repo | — | agent | Claude Sonnet 4.6 |\n"
        )
        assert len(parse(md)) == 1

    def test_table_ends_at_blank_line(self):
        tasks = parse(_SAMPLE)
        # The "## T1 — Thread user_id" section after the table must not be parsed.
        assert all(t["name"].startswith("T") for t in tasks)
        assert len(tasks) == 5

    # -------------------------------------------------------------------------
    # New tests for the 6th Model column
    # -------------------------------------------------------------------------

    def test_model_column_present_in_row(self):
        """Every parsed row must have a 'model' key."""
        tasks = parse(_SAMPLE)
        for t in tasks:
            assert "model" in t

    def test_model_column_populated_for_agent_tasks(self):
        tasks = parse(_SAMPLE)
        t1 = next(t for t in tasks if t["name"] == "T1")
        assert t1["model"] == "Claude Sonnet 4.6"

    def test_model_column_blank_for_human_task(self):
        tasks = parse(_SAMPLE)
        t2 = next(t for t in tasks if t["name"] == "T2")
        assert t2["model"] == ""

    def test_model_column_blank_for_either_task(self):
        tasks = parse(_SAMPLE)
        t3 = next(t for t in tasks if t["name"] == "T3")
        assert t3["model"] == ""

    def test_model_column_different_values(self):
        tasks = parse(_SAMPLE)
        assert tasks[3]["model"] == "Claude Opus 4.8"
        assert tasks[4]["model"] == "Claude Haiku 4.5"

    def test_model_cell_stripped_of_whitespace(self):
        md = (
            "## Index\n\n"
            "| ID | Title | Repo | Depends On | Actor | Model |\n"
            "|----|-------|------|------------|-------|-------|\n"
            "| T1 | Task | my-repo | — | agent |  Claude Sonnet 4.6  |\n"
        )
        tasks = parse(md)
        assert tasks[0]["model"] == "Claude Sonnet 4.6"

    def test_model_dash_normalised_to_empty(self):
        """A '—' or '-' in the Model cell is normalised to empty string."""
        md = (
            "## Index\n\n"
            "| ID | Title | Repo | Depends On | Actor | Model |\n"
            "|----|-------|------|------------|-------|-------|\n"
            "| T1 | Task | my-repo | — | agent | — |\n"
            "| T2 | Task | my-repo | — | human | - |\n"
        )
        tasks = parse(md)
        assert tasks[0]["model"] == ""
        assert tasks[1]["model"] == ""

    # -------------------------------------------------------------------------
    # Required columns only (no optional Model column)
    # -------------------------------------------------------------------------

    def test_required_columns_only_parses(self):
        """A table with only the required columns (no Model) parses successfully."""
        tasks = parse(_SAMPLE_5COL)
        assert len(tasks) == 2
        assert [t["name"] for t in tasks] == ["T1", "T2"]

    def test_required_columns_only_model_blank(self):
        """Every row of a Model-less table gets model == ""."""
        tasks = parse(_SAMPLE_5COL)
        assert all(t["model"] == "" for t in tasks)

    def test_required_columns_only_fields_parse(self):
        """actor_type / depends_on still parse correctly without a Model column."""
        tasks = parse(_SAMPLE_5COL)
        assert tasks[0]["actor_type"] == "agent"
        assert tasks[1]["actor_type"] == "human"
        assert tasks[0]["depends_on"] == []

    def test_required_columns_only_header_case_insensitive(self):
        md = (
            "## Index\n\n"
            "| id | title | repo | depends on | actor |\n"
            "|----|-------|------|------------|-------|\n"
            "| T1 | Task | my-repo | — | agent |\n"
        )
        tasks = parse(md)
        assert len(tasks) == 1
        assert tasks[0]["model"] == ""

    # -------------------------------------------------------------------------
    # Flexible, header-driven layout — order-independent, extra columns ignored
    # -------------------------------------------------------------------------

    def test_reordered_columns_map_by_name(self):
        """Columns in a non-canonical order still map each field correctly."""
        md = (
            "## Index\n\n"
            "| ID | Actor | Title | Repo | Depends On | Model |\n"
            "|----|-------|-------|------|------------|-------|\n"
            "| T1 | human | Do a thing | my-repo | — | |\n"
            "| T2 | agent | Do another | my-repo | T1 | Claude Opus 4.8 |\n"
        )
        tasks = parse(md)
        assert len(tasks) == 2
        assert tasks[0]["actor_type"] == "human"
        assert tasks[0]["title"] == "Do a thing"
        assert tasks[1]["actor_type"] == "agent"
        assert tasks[1]["depends_on"] == ["T1"]
        assert tasks[1]["model"] == "Claude Opus 4.8"

    def test_extra_unknown_column_ignored(self):
        """An added column the parser doesn't know about is ignored, not an error."""
        md = (
            "## Index\n\n"
            "| ID | Title | Priority | Repo | Depends On | Actor | Model |\n"
            "|----|-------|----------|------|------------|-------|-------|\n"
            "| T1 | Task | high | my-repo | — | agent | Claude Sonnet 4.6 |\n"
        )
        tasks = parse(md)
        assert len(tasks) == 1
        assert tasks[0]["title"] == "Task"
        assert tasks[0]["actor_type"] == "agent"
        assert tasks[0]["model"] == "Claude Sonnet 4.6"
        assert "priority" not in tasks[0]


class TestHandle:
    def test_handle_parses_supplied_md(self):
        result = _MOD.handle(tasks_md=_SAMPLE)
        assert result["ok"] is True
        assert result["count"] == 5
        assert result["tasks"][0]["name"] == "T1"

    def test_handle_empty_md_returns_error(self):
        result = _MOD.handle(tasks_md="# Tasks\nno table")
        assert result["ok"] is False
        assert result["tasks"] == []

    def test_handle_model_in_returned_tasks(self):
        result = _MOD.handle(tasks_md=_SAMPLE)
        assert result["ok"] is True
        t1 = next(t for t in result["tasks"] if t["name"] == "T1")
        assert t1["model"] == "Claude Sonnet 4.6"

    def test_handle_required_columns_only_parses(self):
        result = _MOD.handle(tasks_md=_SAMPLE_5COL)
        assert result["ok"] is True
        assert result["count"] == 2
        assert result["tasks"][0]["model"] == ""
