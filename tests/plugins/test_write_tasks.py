"""Tests for write_tasks go-branch: writes tasks.md to storage-service, no git.

Covers:
  - go write_tasks: writes tasks.md to storage-service only — no git touched at all,
    no DB write, no tasks/ YAMLs
  - go write_tasks: return payload has no db_tasks_inserted field
  - go write_tasks: message reflects deferred DB creation (at tasks-stage approval)
  - ts write_tasks: behavior unchanged — tasks.md + per-task YAMLs committed to git
  - ts write_tasks: message reflects YAML files written
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


def _make_workspace_context():
    return {
        "management_repo": "mgmt-repo",
        "repos": [{"id": "mgmt-repo", "github": "git@github.com:org/mgmt.git"}],
    }


def _invoke_handle(owner: str, tasks=None, tasks_md="# Tasks\n"):
    """Call tasks_write.handle with mocked infrastructure."""
    if tasks is None:
        tasks = [
            {"id": "T1", "title": "First task", "repo": "hermes-agent"},
            {"id": "T2", "title": "Second task", "repo": "hermes-agent"},
        ]

    from plugins.tools.tasks_write import handle

    return handle(tasks=tasks, tasks_md=tasks_md, workspace_id="ws-1", feature_id="feat-1")


# ---------------------------------------------------------------------------
# go branch: no DB write, no tasks/ YAMLs
# ---------------------------------------------------------------------------


class TestGoWriteTasksNoDbInsert:
    """go write_tasks writes tasks.md to storage-service only — no git, no DB write."""

    def _run(self):
        write_calls = []

        def fake_write_document_content(workspace_id, feature_id, path, content, **kw):
            write_calls.append((workspace_id, feature_id, path, content))
            return {"ok": True, "version_id": "v1"}

        with (
            patch("plugins.tools.gitnexus.list_indexed_repos", return_value=None),
            patch(
                "plugins.tools.tasks_write.get_feature_detail",
                return_value={
                    "feature_name": "my-feature",
                    "init_pr_url": None,
                    "owner": "go",
                },
            ),
            patch(
                "plugins.tools.tasks_write.write_document_content",
                side_effect=fake_write_document_content,
            ),
            patch(
                "plugins.tools.tasks_write._commit_files",
                side_effect=AssertionError("git must not be touched for go features"),
            ),
        ):
            result = _invoke_handle("go")

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


# ---------------------------------------------------------------------------
# ts branch: tasks.md + per-task YAMLs (regression)
# ---------------------------------------------------------------------------


class TestTsWriteTasksUnchanged:
    """ts write_tasks still commits tasks.md + per-task YAML files."""

    def _run(self):
        committed_files = {}

        def fake_commit_files(gh_owner, gh_repo, branch, files, commit_msg, github_token):
            committed_files.update(files)
            return "def456"

        with (
            patch("plugins.tools.gitnexus.list_indexed_repos", return_value=None),
            patch(
                "plugins.tools.tasks_write.get_feature_detail",
                return_value={
                    "feature_name": "ts-feature",
                    "init_pr_url": None,
                    "owner": "ts",
                },
            ),
            patch(
                "plugins.tools.tasks_write.get_workspace_context",
                return_value=_make_workspace_context(),
            ),
            patch(
                "plugins.tools.artifacts._resolve_management_repo",
                return_value=("org", "mgmt"),
            ),
            patch("plugins.tools.tasks_write.branch_exists", return_value=False),
            patch(
                "plugins.tools.tasks_write._commit_files",
                side_effect=fake_commit_files,
            ),
            patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_test"}),
        ):
            result = _invoke_handle("ts")

        return result, committed_files

    def test_returns_ok_true(self):
        result, _ = self._run()
        assert result["ok"] is True

    def test_tasks_md_committed(self):
        _, committed_files = self._run()
        assert any("tasks.md" in p for p in committed_files)

    def test_per_task_yaml_files_committed(self):
        _, committed_files = self._run()
        yaml_files = [p for p in committed_files if "/tasks/T" in p and p.endswith(".yaml")]
        assert len(yaml_files) == 2, f"Expected 2 task YAML files, got: {yaml_files}"
        task_ids = {p.split("/tasks/")[1].replace(".yaml", "") for p in yaml_files}
        assert task_ids == {"T1", "T2"}

    def test_no_db_tasks_inserted_field(self):
        result, _ = self._run()
        assert "db_tasks_inserted" not in result

    def test_message_mentions_yaml_files(self):
        result, _ = self._run()
        msg = result["message"]
        assert "Task YAML files written" in msg or "tasks/" in msg.lower()

    def test_message_does_not_mention_deferred_db(self):
        result, _ = self._run()
        msg = result["message"]
        assert "tasks-stage approval" not in msg or "Task YAML files written" in msg

    def test_owner_in_result_is_ts(self):
        result, _ = self._run()
        assert result["owner"] == "ts"

    def test_tasks_committed_count(self):
        result, _ = self._run()
        assert result["tasks_committed"] == 2
