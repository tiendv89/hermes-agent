"""Tests for write_tasks: writes tasks.md to storage-service, no git.

Covers:
  - write_tasks: writes tasks.md to storage-service only — no git touched at all,
    no DB write, no tasks/ YAMLs
  - write_tasks: return payload has no db_tasks_inserted field
  - write_tasks: message reflects deferred DB creation (at tasks-stage approval)
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    """Remove plugins modules between tests to avoid cross-test pollution."""
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]


def _invoke_handle(tasks=None, tasks_md="# Tasks\n"):
    """Call tasks_write.handle with mocked infrastructure."""
    if tasks is None:
        tasks = [
            {"id": "T1", "title": "First task", "repo": "hermes-agent"},
            {"id": "T2", "title": "Second task", "repo": "hermes-agent"},
        ]

    from plugins.tools.tasks_write import handle

    return handle(
        tasks=tasks, tasks_md=tasks_md, workspace_id="ws-1", feature_id="feat-1"
    )


# ---------------------------------------------------------------------------
# write_tasks: no DB write, no tasks/ YAMLs
# ---------------------------------------------------------------------------


class TestGoWriteTasksNoDbInsert:
    """write_tasks writes tasks.md to storage-service only — no git, no DB write."""

    def _run(self):
        write_calls = []

        def fake_write_document_content(workspace_id, feature_id, path, content, **kw):
            write_calls.append((workspace_id, feature_id, path, content))
            return {"ok": True, "version_id": "v1"}

        with (
            patch("plugins.tools.gitnexus.list_indexed_repos", return_value=None),
            patch(
                "plugins.tools.tasks_write.write_document_content",
                side_effect=fake_write_document_content,
            ),
        ):
            result = _invoke_handle()

        return result, write_calls

    def test_returns_ok_true(self):
        result, _ = self._run()
        assert result["ok"] is True, result.get("error")

    def test_writes_tasks_md_to_storage_service(self):
        _, write_calls = self._run()
        assert len(write_calls) == 1
        workspace_id, feature_id, path, content = write_calls[0]
        assert workspace_id == "ws-1"
        assert feature_id == "feat-1"
        assert path == "tasks.md"
        assert content == "# Tasks\n"

    def test_no_db_tasks_inserted_field(self):
        result, _ = self._run()
        assert "db_tasks_inserted" not in result

    def test_message_mentions_deferred_db_creation(self):
        result, _ = self._run()
        msg = result["message"]
        assert "tasks-stage approval" in msg or "approve_feature" in msg

    def test_message_does_not_claim_db_stored(self):
        result, _ = self._run()
        msg = result["message"]
        assert "Task state stored in DB" not in msg

    def test_owner_in_result_is_go(self):
        result, _ = self._run()
        assert result["owner"] == "go"

    def test_branch_and_commit_sha_are_none(self):
        result, _ = self._run()
        assert result["branch"] is None
        assert result["commit_sha"] is None

    def test_tasks_committed_count(self):
        result, _ = self._run()
        assert result["tasks_committed"] == 2

    def test_files_written_contains_only_tasks_md(self):
        result, _ = self._run()
        assert result["files_written"] == ["storage-service://ws-1/feat-1/tasks.md"]

    def test_no_db_insert_called(self):
        """Verify _insert_tasks_to_db does not exist on the module."""
        from plugins.tools import tasks_write

        assert not hasattr(tasks_write, "_insert_tasks_to_db"), (
            "_insert_tasks_to_db should have been removed from tasks_write"
        )
