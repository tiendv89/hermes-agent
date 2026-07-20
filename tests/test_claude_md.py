"""Tests that CLAUDE.md contains the required human-actor prompt instruction.

Covers T5 (human-task-actor): the breakdown-phase prompt that directs the agent
to ask the human which tasks are human-owned.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CLAUDE_MD_PATH = REPO_ROOT / "CLAUDE.md"


def _read_claude_md() -> str:
    return CLAUDE_MD_PATH.read_text(encoding="utf-8")


class TestClaudeMdHumanActorPrompt:
    """Verify CLAUDE.md instructs the agent to prompt for human-owned tasks
    during the task-breakdown phase (write_tasks)."""

    def test_claude_md_exists(self):
        assert CLAUDE_MD_PATH.is_file(), "CLAUDE.md must exist at repo root"

    def test_contains_human_actor_question(self):
        content = _read_claude_md()
        assert (
            "Are any of these tasks meant to be done by a human"
            in content
        ), "CLAUDE.md must contain the prompt asking about human-owned tasks"

    def test_mentions_write_tasks_phase(self):
        content = _read_claude_md()
        assert (
            "write_tasks" in content
        ), "CLAUDE.md must mention the write_tasks / task-breakdown phase"

    def test_defaults_to_agent_when_human_says_none(self):
        content = _read_claude_md()
        assert (
            "actor_type: agent" in content
        ), "CLAUDE.md must specify that agent is the default actor_type"

    def test_actor_column_required_in_index_table(self):
        content = _read_claude_md()
        assert (
            "| ID | Title | Repo | Depends On | Actor |" in content
        ), "CLAUDE.md must show the required Index table columns including Actor"

    def test_human_tasks_model_blank(self):
        content = _read_claude_md()
        assert (
            "blank" in content.lower()
        ), "CLAUDE.md must specify that Model cell should be blank for human tasks"
