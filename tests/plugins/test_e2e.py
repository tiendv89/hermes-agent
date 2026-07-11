"""E2E integration tests

Tests simulate multi-call sequences and state transitions across multiple tool invocations,
distinguishing them from the unit tests in test_approve_t5.py and test_create_tasks_t6.py.

Subtask coverage:
  1. Full pipeline: spec/design approve → breakdown (tasks.md only, no DB write) →
     tasks approve (a→b→c→d) → tasks in DB via API.
  2. Resumable approve: inject failure at b → re-run completes a-skipped + b→c→d;
     inject failure at d → re-run detects already-on-base + idempotent c + d.
  3. Backup /create-tasks: guard reject notification (feature_not_tasks_approved);
     success; tasks-exist no-op.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    """Remove plugins/src modules between tests to avoid cross-test pollution."""
    keys = [k for k in sys.modules if k.startswith("plugins") or k.startswith("src")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins") or k.startswith("src")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_TOKEN = "ghp_test_e2e"
_WORKSPACE_ID = "ws-e2e"
_FEATURE_ID = "e2e-feature"
_OWNER = "testorg"
_REPO = "testws"
_BASE_BRANCH = "main"
_FEATURE_BRANCH = "feature/e2e-feature"
_ACTOR = "agent@e2e.test"

_TASKS_MD = """\
# Tasks — `e2e-feature`

## Index

| ID | Title | Repo | Depends On | Actor |
|----|-------|------|------------|-------|
| T1 | First task | hermes-agent | — | agent |
| T2 | Second task | hermes-agent | T1 | agent |

## T1 — First task

### Description
First task.

## T2 — Second task

### Description
Second task.
"""

# status.yaml with tasks stage in draft (before first approve call)
_STATUS_YAML_TASKS_DRAFT = """\
feature_id: e2e-feature
feature_status: in_tdd
current_stage: tasks
next_action: Awaiting tasks approval.
stages:
  tasks:
    review_status: draft
    reviewed_by: null
    reviewed_at: null
    review_comment: null
    review_history: []
history: []
"""

# status.yaml after step a committed it (tasks approved state)
_STATUS_YAML_TASKS_APPROVED = """\
feature_id: e2e-feature
feature_status: ready_for_implementation
current_stage: handoff
next_action: Tasks ready for implementation.
stages:
  tasks:
    review_status: approved
    reviewed_by: agent@e2e.test
    reviewed_at: 2026-07-03T12:00:00+0000
    review_comment: null
    review_history:
      - review_status: approved
        reviewed_by: agent@e2e.test
        reviewed_at: 2026-07-03T12:00:00+0000
history: []
"""

# status.yaml for product_spec stage
_STATUS_YAML_PRODUCT_SPEC_DRAFT = """\
feature_id: e2e-feature
feature_status: in_design
current_stage: product_spec
next_action: Awaiting product spec approval.
stages:
  product_spec:
    review_status: draft
    reviewed_by: null
    reviewed_at: null
    review_comment: null
    review_history: []
history: []
"""

# status.yaml for technical_design stage
_STATUS_YAML_TECHNICAL_DESIGN_DRAFT = """\
feature_id: e2e-feature
feature_status: in_tdd
current_stage: technical_design
next_action: Awaiting technical design approval.
stages:
  technical_design:
    review_status: draft
    reviewed_by: null
    reviewed_at: null
    review_comment: null
    review_history: []
history: []
"""

_STATUS_SHA = "sha_status_e2e"
_TASKS_MD_SHA = "sha_tasks_e2e"
_COMMIT_SHA = "commit_e2e_abc"


# ---------------------------------------------------------------------------
# Stand-in WorkflowBackendError (avoids importing the real module)
# ---------------------------------------------------------------------------


class _WorkflowBackendError(Exception):
    """Minimal stand-in for WorkflowBackendError used in tests."""

    def __init__(self, message: str, *, reason_code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_workspace_context():
    return {
        "management_repo": _REPO,
        "repos": [{"id": _REPO, "github": f"https://github.com/{_OWNER}/{_REPO}"}],
    }


def _make_feature_detail(owner="go", stage="tasks"):
    return {
        "feature_name": _FEATURE_ID,
        "title": "E2E Feature",
        "stage": stage,
        "status": "in_tdd",
        "next_action": "Awaiting approval.",
        "owner": owner,
        "init_pr_url": None,
    }


def _make_read_document(status_content=_STATUS_YAML_TASKS_DRAFT, tasks_content=_TASKS_MD):
    """Return a side_effect for read_document dispatching by path suffix."""

    def _read_doc(gh_owner, gh_repo, branch, path, github_token):
        if path.endswith("tasks.md"):
            return {"content": tasks_content, "sha": _TASKS_MD_SHA}
        if path.endswith("status.yaml"):
            return {"content": status_content, "sha": _STATUS_SHA}
        return {"content": "", "sha": None}

    return _read_doc


def _load_approve_mod():
    """Load plugins.tools.approve fresh (relies on _clean_modules fixture)."""
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(REPO_ROOT / "plugins")]
        pkg.__package__ = "plugins"
        sys.modules["plugins"] = pkg

    spec = importlib.util.spec_from_file_location(
        "plugins.tools.approve",
        REPO_ROOT / "plugins" / "tools" / "approve.py",
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "plugins.tools"
    sys.modules["plugins.tools.approve"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_create_tasks_mod():
    """Load plugins.tools.create_tasks fresh (relies on _clean_modules fixture)."""
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(REPO_ROOT / "plugins")]
        pkg.__package__ = "plugins"
        sys.modules["plugins"] = pkg

    spec = importlib.util.spec_from_file_location(
        "plugins.tools.create_tasks",
        REPO_ROOT / "plugins" / "tools" / "create_tasks.py",
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "plugins.tools"
    sys.modules["plugins.tools.create_tasks"] = mod
    spec.loader.exec_module(mod)
    return mod


def _make_open_pr(number=1):
    return {"number": number, "html_url": f"https://github.com/{_OWNER}/{_REPO}/pull/{number}"}


def _setup_create_tasks_sys_modules(
    *,
    create_mock,
    tasks_content=_TASKS_MD,
    wbc_error_class=_WorkflowBackendError,
):
    """Inject all sys.modules stubs needed by create_tasks.handle()."""
    from src.services.workflow_backend_client import run_async as _real_run_async

    ctx_mock = MagicMock()
    ctx_mock.get_workspace_id.return_value = _WORKSPACE_ID
    ctx_mock.get_feature_id.return_value = _FEATURE_ID
    ctx_mock.get_user_id.return_value = ""
    ctx_mock.get_org_id.return_value = ""
    # run_async checks get_agent_loop() to decide sync-fallback vs cross-thread
    # scheduling — must be None here so it takes the asyncio.run() path.
    ctx_mock.get_agent_loop.return_value = None
    sys.modules["plugins.context"] = ctx_mock

    doc_repo_mock = MagicMock()
    doc_repo_mock.read_document.return_value = {"content": tasks_content, "sha": _TASKS_MD_SHA}
    sys.modules["plugins.document_repo"] = doc_repo_mock

    approve_mock = MagicMock()
    approve_mock._resolve_status_branch_and_path.return_value = (
        _FEATURE_BRANCH,
        f"docs/features/{_FEATURE_ID}/status.yaml",
    )
    approve_mock._run_async_create_tasks = create_mock
    sys.modules["plugins.tools.approve"] = approve_mock

    artifacts_mock = MagicMock()
    artifacts_mock._resolve_management_repo.return_value = (_OWNER, _REPO)
    sys.modules["plugins.tools.artifacts"] = artifacts_mock

    async def _fake_get_workspace_context(*_a, **_kw):
        return _make_workspace_context()

    async def _fake_get_feature_detail(*_a, **_kw):
        return _make_feature_detail()

    wbc_mock = MagicMock()
    wbc_mock.WorkflowBackendError = wbc_error_class
    wbc_mock.get_workspace_context = _fake_get_workspace_context
    wbc_mock.get_feature_detail = _fake_get_feature_detail
    wbc_mock.run_async = _real_run_async
    sys.modules["src.services.workflow_backend_client"] = wbc_mock


# ---------------------------------------------------------------------------
# E2E Subtask 1 — Full pipeline: spec/design approve → write_tasks (no DB) →
#                 tasks approve (a→b→c→d) → tasks in DB via API
# ---------------------------------------------------------------------------


class TestE2EFullPipeline:
    """Simulate the complete feature lifecycle: approve each stage in sequence,
    verify write_tasks produces tasks.md only, then verify tasks-stage approve
    runs the full a→b→c→d pipeline and creates tasks via the API.
    """

    def _approve_stage_go(self, monkeypatch, stage, status_content):
        """Run approve.handle() for a go feature at the given stage with mocked deps."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        update_mock = AsyncMock()
        commit_to_branch_mock = MagicMock(return_value=_COMMIT_SHA)

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail(stage=stage))
        mod.update_feature_stage = update_mock
        mod.read_document = MagicMock(
            return_value={"content": status_content, "sha": _STATUS_SHA}
        )
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod.commit_to_branch = commit_to_branch_mock

        with (
            patch("plugins.document_repo.branch_exists", return_value=True),
            patch("plugins.tools.tasks_write._commit_files", MagicMock()),
        ):
            result = mod.handle(
                stage=stage,
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        return result, update_mock, commit_to_branch_mock

    def test_product_spec_approve_uses_db_only_for_go_feature(self, monkeypatch):
        """go feature: product_spec approve calls DB update, no a→b→c→d pipeline."""
        result, update_mock, commit_mock = self._approve_stage_go(
            monkeypatch, "product_spec", _STATUS_YAML_PRODUCT_SPEC_DRAFT
        )
        assert result["ok"] is True
        update_mock.assert_called_once()
        commit_mock.assert_not_called()

    def test_technical_design_approve_uses_db_only_for_go_feature(self, monkeypatch):
        """go feature: technical_design approve calls DB update, no pipeline."""
        result, update_mock, commit_mock = self._approve_stage_go(
            monkeypatch, "technical_design", _STATUS_YAML_TECHNICAL_DESIGN_DRAFT
        )
        assert result["ok"] is True
        update_mock.assert_called_once()
        commit_mock.assert_not_called()

    def test_write_tasks_go_branch_no_db_insert(self, monkeypatch):
        """write_tasks for go features commits tasks.md only — no DB write."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)

        committed_files = {}

        def fake_commit_files(gh_owner, gh_repo, branch, files, commit_msg, github_token):
            committed_files.update(files)
            return _COMMIT_SHA

        with (
            patch("plugins.tools.gitnexus.list_indexed_repos", return_value=None),
            patch(
                "plugins.tools.tasks_write.get_feature_detail",
                return_value={
                    "feature_name": _FEATURE_ID,
                    "init_pr_url": None,
                    "owner": "go",
                },
            ),
            patch(
                "plugins.tools.tasks_write.get_workspace_context",
                return_value=_make_workspace_context(),
            ),
            patch(
                "plugins.tools.artifacts._resolve_management_repo",
                return_value=(_OWNER, _REPO),
            ),
            patch(
                "plugins.tools.tasks_write.branch_exists",
                return_value=False,
            ),
            patch(
                "plugins.tools.tasks_write._commit_files",
                side_effect=fake_commit_files,
            ),
            patch.dict("os.environ", {"GITHUB_TOKEN": _GITHUB_TOKEN}),
        ):
            from plugins.tools.tasks_write import handle as write_tasks_handle

            tasks_input = [
                {"id": "T1", "title": "First task", "repo": "hermes-agent"},
                {"id": "T2", "title": "Second task", "repo": "hermes-agent"},
            ]
            result = write_tasks_handle(
                tasks=tasks_input,
                tasks_md=_TASKS_MD,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result.get("ok") is True, result.get("error")
        assert any("tasks.md" in p for p in committed_files), "tasks.md not committed"
        yaml_files = [p for p in committed_files if "/tasks/T" in p and p.endswith(".yaml")]
        assert yaml_files == [], f"go branch must not commit task YAMLs: {yaml_files}"
        assert result.get("db_tasks_inserted") is None

    def test_tasks_stage_approve_runs_full_pipeline_a_b_c_d(self, monkeypatch):
        """Tasks-stage approve (go) runs all four steps in order and succeeds."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        commit_mock = MagicMock(return_value=_COMMIT_SHA)
        merge_mock = MagicMock()
        update_mock = AsyncMock()
        create_tasks_mock = MagicMock(return_value={"tasks": [{"id": "T1"}, {"id": "T2"}]})
        activate_mock = MagicMock(return_value=["T1"])
        open_prs = [_make_open_pr(1)]

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail())
        mod.update_feature_stage = update_mock
        mod.read_document = MagicMock(side_effect=_make_read_document())
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._read_status_yaml_on_branch = MagicMock(return_value=None)
        mod._find_open_prs = MagicMock(return_value=open_prs)
        mod._merge_pr = merge_mock
        mod._run_async_create_tasks = create_tasks_mock
        mod._activate_tasks_db = activate_mock

        with (
            patch("plugins.document_repo.branch_exists", return_value=False),
            patch("plugins.tools.tasks_write._commit_files", commit_mock),
        ):
            mod.commit_to_branch = MagicMock(return_value=_COMMIT_SHA)
            result = mod.handle(
                stage="tasks",
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True, result.get("error")
        # Step a: commit
        commit_mock.assert_called_once()
        # Step b: PR merged
        merge_mock.assert_called_once_with(_OWNER, _REPO, 1, _GITHUB_TOKEN)
        # Step c: DB update
        update_mock.assert_called_once()
        update_call = update_mock.call_args[1]
        assert update_call["stage"] == "tasks"
        assert update_call["review_status"] == "approved"
        # Step d: tasks created via API — parsed task list (not raw tasks.md)
        create_tasks_mock.assert_called_once()
        d_call = create_tasks_mock.call_args
        assert d_call.args[0] == _WORKSPACE_ID
        assert d_call.args[1] == _FEATURE_ID
        assert [t["name"] for t in d_call.args[2]] == ["T1", "T2"]

    def test_tasks_stage_approve_result_includes_activated_tasks(self, monkeypatch):
        """After the full pipeline, the result reports activated tasks."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        activated = ["T1", "T2"]

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail())
        mod.update_feature_stage = AsyncMock()
        mod.read_document = MagicMock(side_effect=_make_read_document())
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._read_status_yaml_on_branch = MagicMock(return_value=None)
        mod._find_open_prs = MagicMock(return_value=[_make_open_pr(2)])
        mod._merge_pr = MagicMock()
        mod._run_async_create_tasks = MagicMock(return_value={"tasks": []})
        mod._activate_tasks_db = MagicMock(return_value=activated)

        with (
            patch("plugins.document_repo.branch_exists", return_value=False),
            patch("plugins.tools.tasks_write._commit_files", MagicMock(return_value=_COMMIT_SHA)),
        ):
            mod.commit_to_branch = MagicMock(return_value=_COMMIT_SHA)
            result = mod.handle(
                stage="tasks",
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result.get("activated_tasks") == activated


# ---------------------------------------------------------------------------
# E2E Subtask 2 — Resumable approve: failure at b → re-run; failure at d → re-run
# ---------------------------------------------------------------------------


class TestE2EResumableApproveAfterStepBFailure:
    """Simulate: first call to handle() fails at step b; second call resumes from b."""

    def _first_call_fails_at_b(self, monkeypatch):
        """First approve call fails at step b (zero matching PRs)."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        commit_mock = MagicMock(return_value=_COMMIT_SHA)
        update_mock = AsyncMock()
        create_mock = MagicMock()

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail())
        mod.update_feature_stage = update_mock
        mod.read_document = MagicMock(side_effect=_make_read_document(_STATUS_YAML_TASKS_DRAFT))
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._read_status_yaml_on_branch = MagicMock(return_value=None)
        mod._find_open_prs = MagicMock(return_value=[])  # zero PRs → halt at b
        mod._merge_pr = MagicMock()
        mod._run_async_create_tasks = create_mock
        mod._activate_tasks_db = MagicMock(return_value=[])

        with (
            patch("plugins.document_repo.branch_exists", return_value=False),
            patch("plugins.tools.tasks_write._commit_files", commit_mock),
        ):
            mod.commit_to_branch = MagicMock(return_value=_COMMIT_SHA)
            result = mod.handle(
                stage="tasks",
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        return result, commit_mock, update_mock, create_mock

    def test_first_call_fails_at_b_with_ok_false(self, monkeypatch):
        result, _, _, _ = self._first_call_fails_at_b(monkeypatch)
        assert result["ok"] is False
        assert result.get("failed_step") == "b"

    def test_first_call_commits_step_a_before_b_failure(self, monkeypatch):
        """Step a (git commit) runs even though step b fails — it runs first."""
        result, commit_mock, _, _ = self._first_call_fails_at_b(monkeypatch)
        assert result["failed_step"] == "b"
        commit_mock.assert_called_once()

    def test_first_call_does_not_call_update_stage_on_b_failure(self, monkeypatch):
        """Step c (DB update) is never reached when step b fails."""
        result, _, update_mock, _ = self._first_call_fails_at_b(monkeypatch)
        assert result["failed_step"] == "b"
        update_mock.assert_not_called()

    def test_first_call_does_not_create_tasks_on_b_failure(self, monkeypatch):
        """Step d (create tasks) is never reached when step b fails."""
        result, _, _, create_mock = self._first_call_fails_at_b(monkeypatch)
        assert result["failed_step"] == "b"
        create_mock.assert_not_called()

    def _second_call_after_b_failure(self, monkeypatch, open_prs=None):
        """Second call with status.yaml already showing approved (step a persisted)."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        if open_prs is None:
            open_prs = [_make_open_pr(10)]

        commit_mock = MagicMock(return_value=_COMMIT_SHA)
        update_mock = AsyncMock()
        create_mock = MagicMock(return_value={"tasks": []})
        merge_mock = MagicMock()

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail())
        mod.update_feature_stage = update_mock
        # Status now shows approved (step a committed it in first call)
        mod.read_document = MagicMock(
            side_effect=_make_read_document(_STATUS_YAML_TASKS_APPROVED)
        )
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._read_status_yaml_on_branch = MagicMock(return_value=None)
        mod._find_open_prs = MagicMock(return_value=open_prs)
        mod._merge_pr = merge_mock
        mod._run_async_create_tasks = create_mock
        mod._activate_tasks_db = MagicMock(return_value=["T1"])

        with (
            patch("plugins.document_repo.branch_exists", return_value=False),
            patch("plugins.tools.tasks_write._commit_files", commit_mock),
        ):
            mod.commit_to_branch = MagicMock(return_value=_COMMIT_SHA)
            result = mod.handle(
                stage="tasks",
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        return result, commit_mock, update_mock, merge_mock, create_mock

    def test_second_call_skips_step_a_and_completes(self, monkeypatch):
        """Re-run with status.yaml already approved: step a skipped, b→c→d complete."""
        result, commit_mock, update_mock, merge_mock, create_mock = (
            self._second_call_after_b_failure(monkeypatch)
        )
        assert result["ok"] is True, result.get("error")
        commit_mock.assert_not_called()
        merge_mock.assert_called_once()
        update_mock.assert_called_once()
        create_mock.assert_called_once()

    def test_second_call_result_ok_true_no_failed_step(self, monkeypatch):
        """Re-run after step b failure: result ok=True and no failed_step."""
        result, _, _, _, _ = self._second_call_after_b_failure(monkeypatch)
        assert result["ok"] is True
        assert "failed_step" not in result


class TestE2EResumableApproveAfterStepDFailure:
    """Simulate: first call fails at step d; second call resumes and completes without duplication."""

    def _second_call_after_d_failure(
        self, monkeypatch, *, create_tasks_side_effect=None
    ):
        """Run approve after d-failure: status approved, docs already on base.

        Patches src.services.workflow_backend_client so that inline
        ``from src.services.workflow_backend_client import WorkflowBackendError``
        inside approve.handle() resolves to our _WorkflowBackendError stand-in.
        This lets callers raise _WorkflowBackendError and have it caught correctly.
        """
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        import yaml as _yaml

        if create_tasks_side_effect is None:
            create_tasks_side_effect = MagicMock(return_value={"tasks": []})

        commit_mock = MagicMock(return_value=_COMMIT_SHA)
        update_mock = AsyncMock()
        merge_mock = MagicMock()

        # Parse the approved status for the "already on base" check
        _approved_status = _yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)

        # Inject the workflow_backend_client stub so the inline import inside
        # approve.handle() gets our _WorkflowBackendError stand-in. run_async
        # must stay the real bridge (approve.py's module-level `run_async`
        # name is bound to this stub at reload time, and its own callers
        # below only override get_workspace_context/get_feature_detail/
        # update_feature_stage, not run_async itself).
        from src.services.workflow_backend_client import run_async as _real_run_async

        wbc_stub = MagicMock()
        wbc_stub.WorkflowBackendError = _WorkflowBackendError
        wbc_stub.run_async = _real_run_async
        sys.modules["src.services.workflow_backend_client"] = wbc_stub

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail())
        mod.update_feature_stage = update_mock
        # Status already approved (step a already done)
        mod.read_document = MagicMock(
            side_effect=_make_read_document(_STATUS_YAML_TASKS_APPROVED)
        )
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        # Step b: docs already on base → skip (return approved status)
        mod._read_status_yaml_on_branch = MagicMock(return_value=_approved_status)
        mod._find_open_prs = MagicMock(return_value=[])
        mod._merge_pr = merge_mock
        mod._run_async_create_tasks = create_tasks_side_effect
        mod._activate_tasks_db = MagicMock(return_value=[])

        with (
            patch("plugins.document_repo.branch_exists", return_value=False),
            patch("plugins.tools.tasks_write._commit_files", commit_mock),
        ):
            mod.commit_to_branch = MagicMock(return_value=_COMMIT_SHA)
            result = mod.handle(
                stage="tasks",
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        return result, commit_mock, update_mock, merge_mock, create_tasks_side_effect

    def test_second_call_skips_step_a(self, monkeypatch):
        """After d-failure, re-run with approved status: step a (commit) is skipped."""
        result, commit_mock, _, _, _ = self._second_call_after_d_failure(monkeypatch)
        commit_mock.assert_not_called()

    def test_second_call_skips_step_b_when_already_on_base(self, monkeypatch):
        """After d-failure, re-run: step b skips (docs already on base), no PR merge."""
        _, _, _, merge_mock, _ = self._second_call_after_d_failure(monkeypatch)
        merge_mock.assert_not_called()

    def test_second_call_runs_step_c(self, monkeypatch):
        """After d-failure, re-run: step c (DB update) still runs (idempotent)."""
        _, _, update_mock, _, _ = self._second_call_after_d_failure(monkeypatch)
        update_mock.assert_called_once()

    def test_second_call_runs_step_d_and_succeeds(self, monkeypatch):
        """After d-failure, re-run: step d (create tasks) runs and succeeds."""
        create_mock = MagicMock(return_value={"tasks": [{"id": "T1"}, {"id": "T2"}]})
        result, _, _, _, _ = self._second_call_after_d_failure(
            monkeypatch, create_tasks_side_effect=create_mock
        )
        assert result["ok"] is True
        create_mock.assert_called_once()

    def test_second_call_tasks_already_exist_is_safe_no_op(self, monkeypatch):
        """After d-failure, if tasks were already created (by another path), it's a safe no-op."""
        already_exist = MagicMock(
            side_effect=_WorkflowBackendError(
                "tasks already exist",
                reason_code="tasks_already_exist",
                status=409,
            )
        )
        result, _, _, _, _ = self._second_call_after_d_failure(
            monkeypatch, create_tasks_side_effect=already_exist
        )
        # tasks_already_exist is treated as ok=True (idempotent)
        assert result["ok"] is True
        assert result.get("failed_step") is None

    def test_second_call_no_duplication_when_tasks_exist(self, monkeypatch):
        """Re-run after d-failure with tasks_already_exist: does not retry creation."""
        create_mock = MagicMock(
            side_effect=_WorkflowBackendError(
                "tasks already exist",
                reason_code="tasks_already_exist",
                status=409,
            )
        )
        result, _, _, _, _ = self._second_call_after_d_failure(
            monkeypatch, create_tasks_side_effect=create_mock
        )
        # Called once (attempted), not zero
        create_mock.assert_called_once()
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# E2E Subtask 3 — Backup /create-tasks: guard reject; success; tasks-exist no-op
# ---------------------------------------------------------------------------


def _run_create_tasks_handle(monkeypatch, *, create_mock, tasks_content=_TASKS_MD):
    """Run create_tasks.handle() with all dependencies mocked via sys.modules."""
    monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
    monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

    mod = _load_create_tasks_mod()
    _setup_create_tasks_sys_modules(create_mock=create_mock, tasks_content=tasks_content)
    result = mod.handle(workspace_id=_WORKSPACE_ID, feature_id=_FEATURE_ID)
    return result, create_mock


class TestE2EBackupCreateTasksGuardReject:
    """Backup /create-tasks: feature_not_tasks_approved → guidance sent, no creation."""

    def _run(self, monkeypatch):
        guard_error = _WorkflowBackendError(
            "feature not tasks approved",
            reason_code="feature_not_tasks_approved",
            status=403,
        )
        create_mock = MagicMock(side_effect=guard_error)
        return _run_create_tasks_handle(monkeypatch, create_mock=create_mock)

    def test_guard_reject_returns_ok_false(self, monkeypatch):
        result, _ = self._run(monkeypatch)
        assert result["ok"] is False

    def test_guard_reject_surfaces_reason_code(self, monkeypatch):
        result, _ = self._run(monkeypatch)
        assert result.get("reason_code") == "feature_not_tasks_approved"

    def test_guard_reject_error_mentions_approve_command(self, monkeypatch):
        result, _ = self._run(monkeypatch)
        err = result.get("error", "")
        assert "approve" in err.lower(), f"Expected 'approve' in error, got: {err!r}"

    def test_guard_reject_error_mentions_abc_steps(self, monkeypatch):
        result, _ = self._run(monkeypatch)
        err = result.get("error", "")
        # The guidance should mention steps a→b→c
        has_steps = "a→b→c" in err or "a→b" in err or ("steps" in err.lower())
        assert has_steps, f"Expected step references in error, got: {err!r}"

    def test_guard_reject_creation_attempted_once(self, monkeypatch):
        """The creation was attempted (and rejected by the guard) exactly once."""
        _, create_mock = self._run(monkeypatch)
        create_mock.assert_called_once()


class TestE2EBackupCreateTasksSuccess:
    """Backup /create-tasks: success path — tasks created via API."""

    def _run(self, monkeypatch):
        create_result = {"tasks": [{"id": "T1"}, {"id": "T2"}]}
        create_mock = MagicMock(return_value=create_result)
        return _run_create_tasks_handle(monkeypatch, create_mock=create_mock)

    def test_success_returns_ok_true(self, monkeypatch):
        result, _ = self._run(monkeypatch)
        assert result["ok"] is True

    def test_success_message_present(self, monkeypatch):
        result, _ = self._run(monkeypatch)
        assert "message" in result
        msg = result["message"]
        assert "success" in msg.lower() or "created" in msg.lower(), (
            f"Expected 'success' or 'created' in message, got: {msg!r}"
        )

    def test_success_calls_create_tasks_with_workspace_and_feature(self, monkeypatch):
        _, create_mock = self._run(monkeypatch)
        create_mock.assert_called_once()
        args = create_mock.call_args[0]
        assert args[0] == _WORKSPACE_ID
        assert args[1] == _FEATURE_ID

    def test_success_passes_parsed_tasks(self, monkeypatch):
        _, create_mock = self._run(monkeypatch)
        tasks_arg = create_mock.call_args[0][2]
        # The parsed task list (not the raw tasks.md) is passed to create.
        assert isinstance(tasks_arg, list)
        names = [t["name"] for t in tasks_arg]
        assert names == ["T1", "T2"]

    def test_success_result_included_in_response(self, monkeypatch):
        result, _ = self._run(monkeypatch)
        assert result.get("result") is not None


class TestE2EBackupCreateTasksAlreadyExist:
    """Backup /create-tasks: tasks_already_exist → safe no-op."""

    def _run(self, monkeypatch):
        noop_error = _WorkflowBackendError(
            "tasks already exist",
            reason_code="tasks_already_exist",
            status=409,
        )
        create_mock = MagicMock(side_effect=noop_error)
        return _run_create_tasks_handle(monkeypatch, create_mock=create_mock)

    def test_tasks_exist_returns_ok_true(self, monkeypatch):
        """tasks_already_exist is a safe no-op: ok=True."""
        result, _ = self._run(monkeypatch)
        assert result["ok"] is True

    def test_tasks_exist_sets_noop_flag(self, monkeypatch):
        result, _ = self._run(monkeypatch)
        assert result.get("noop") is True

    def test_tasks_exist_message_says_nothing_to_do(self, monkeypatch):
        result, _ = self._run(monkeypatch)
        msg = result.get("message", "")
        assert "exist" in msg.lower() or "nothing" in msg.lower(), (
            f"Expected 'exist' or 'nothing' in message, got: {msg!r}"
        )

    def test_tasks_exist_does_not_surface_reason_code_as_error(self, monkeypatch):
        """A tasks_already_exist response must not include an 'error' key."""
        result, _ = self._run(monkeypatch)
        assert "error" not in result, f"Unexpected 'error' key in result: {result}"

    def test_tasks_exist_creation_attempted_once(self, monkeypatch):
        """Creation is attempted once (guard fires, but no retry or duplication)."""
        _, create_mock = self._run(monkeypatch)
        create_mock.assert_called_once()
