"""Tests for the parse_tasks tool — strict tasks.md Index-table parsing.

Grammar under test (go features), strict and positional:

    | ID | Title | Repo | Depends On | Actor |

Parsed rows map onto the workflow-backend CreateTaskItem contract:
    name, title, repo, depends_on, actor_type.
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


_SAMPLE = """\
# Tasks — demo

## Index

| ID | Title | Repo | Depends On | Actor |
|----|-------|------|------------|-------|
| T1 | Thread `user_id` into context | hermes-agent | — | agent |
| T2 | Stop at `tasks.md` | hermes-agent | — | human |
| T3 | Server-side guard | workflow-backend | — | either |
| T4 | Service client | hermes-agent | T1 | agent |
| T5 | Approve orchestration | hermes-agent | T2, T3, T4 | agent |

## T1 — Thread user_id
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
            "| ID | Title | Repo | Depends On | Actor |\n"
            "|----|-------|------|------------|-------|\n"
            "| T1 | Task | my-repo | — | robot |\n"
        )
        assert parse(md)[0]["actor_type"] == "agent"

    def test_blank_actor_defaults_to_agent(self):
        md = (
            "## Index\n\n"
            "| ID | Title | Repo | Depends On | Actor |\n"
            "|----|-------|------|------------|-------|\n"
            "| T1 | Task | my-repo | — |  |\n"
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
            "| id | title | repo | depends on | actor |\n"
            "|----|-------|------|------------|-------|\n"
            "| T1 | Task | my-repo | — | agent |\n"
        )
        assert len(parse(md)) == 1

    def test_table_ends_at_blank_line(self):
        tasks = parse(_SAMPLE)
        # The "## T1 — Thread user_id" section after the table must not be parsed.
        assert all(t["name"].startswith("T") for t in tasks)
        assert len(tasks) == 5


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
