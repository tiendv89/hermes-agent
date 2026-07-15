"""E2E integration tests

Tests simulate multi-call sequences and state transitions across multiple tool invocations,
distinguishing them from the unit tests in test_approve_t5.py and test_create_tasks_t6.py.

Subtask coverage:
  1. Full pipeline (go): spec/design approve (DB-only) → breakdown (tasks.md to
     storage-service) → tasks approve (DB update, then create tasks read from
     storage-service) → tasks in DB via API. No git anywhere for go.
  2. Resumable approve: inject failure at the DB update step (formerly "step c")
     → re-run completes idempotently (DB update + task creation/activation).
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

_WORKSPACE_ID = "ws-e2e"
_FEATURE_ID = "e2e-feature"
_OWNER = "testorg"
_REPO = "testws"
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


def _make_feature_detail(owner="go", stage="tasks", stages=None):
    return {
        "feature_name": _FEATURE_ID,
        "title": "E2E Feature",
        "stage": stage,
        "status": "in_tdd",
        "next_action": "Awaiting approval.",
        "owner": owner,
        "init_pr_url": None,
        "stages": stages if stages is not None else {},
    }


# Per-stage "stages" JSONB for a go feature with tasks stage in draft (before
# first approve call) — mirrors workflow-backend's workspace_features table.
_STAGES_TASKS_DRAFT = {
    "tasks": {
        "review_status": "draft",
        "reviewed_by": None,
        "reviewed_at": None,
        "review_comment": None,
        "review_history": [],
    }
}

# "stages" for a feature that already shows tasks approved (resume testing).
_STAGES_TASKS_APPROVED = {
    "tasks": {
        "review_status": "approved",
        "reviewed_by": "agent@e2e.test",
        "reviewed_at": "2026-07-03T12:00:00+0000",
        "review_comment": None,
        "review_history": [
            {
                "review_status": "approved",
                "reviewed_by": "agent@e2e.test",
                "reviewed_at": "2026-07-03T12:00:00+0000",
                "comment": None,
            }
        ],
    }
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

    # tasks.md is read from storage-service.
    storage_mock = MagicMock()
    storage_mock.read_document_content.return_value = {
        "content": tasks_content,
        "version_id": "v1",
    }
    sys.modules["plugins.clients.storage_service_client"] = storage_mock

    approve_mock = MagicMock()
    approve_mock._run_async_create_tasks = create_mock
    sys.modules["plugins.tools.approve"] = approve_mock

    async def _fake_get_feature_detail(*_a, **_kw):
        return _make_feature_detail()

    wbc_mock = MagicMock()
    wbc_mock.WorkflowBackendError = wbc_error_class
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

    def _approve_stage_go(self, monkeypatch, stage, stages):
        """Run approve.handle() for the given stage with mocked deps."""
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)

        update_mock = AsyncMock()

        mod = _load_approve_mod()
        mod.get_feature_detail = AsyncMock(
            return_value=_make_feature_detail(stage=stage, stages=stages)
        )
        mod.update_feature_stage = update_mock

        result = mod.handle(
            stage=stage,
            action="approve",
            workspace_id=_WORKSPACE_ID,
            feature_id=_FEATURE_ID,
        )

        return result, update_mock

    def test_product_spec_approve_uses_db_only_for_go_feature(self, monkeypatch):
        """go feature: product_spec approve calls DB update only — no git."""
        stages = {
            "product_spec": {
                "review_status": "draft",
                "reviewed_by": None,
                "reviewed_at": None,
                "review_comment": None,
                "review_history": [],
            }
        }
        result, update_mock = self._approve_stage_go(monkeypatch, "product_spec", stages)
        assert result["ok"] is True, result.get("error")
        update_mock.assert_called_once()

    def test_technical_design_approve_uses_db_only_for_go_feature(self, monkeypatch):
        """go feature: technical_design approve calls DB update only — no git."""
        stages = {
            "technical_design": {
                "review_status": "draft",
                "reviewed_by": None,
                "reviewed_at": None,
                "review_comment": None,
                "review_history": [],
            }
        }
        result, update_mock = self._approve_stage_go(
            monkeypatch, "technical_design", stages
        )
        assert result["ok"] is True, result.get("error")
        update_mock.assert_called_once()

    def test_write_tasks_go_branch_no_git(self, monkeypatch):
        """write_tasks writes tasks.md to storage-service — no git."""
        write_content_mock = MagicMock(return_value={"version_id": "v1"})

        with (
            patch("plugins.tools.gitnexus.list_indexed_repos", return_value=None),
            patch("plugins.tools.tasks_write.write_document_content", write_content_mock),
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
        write_content_mock.assert_called_once()
        call = write_content_mock.call_args
        assert call.args[0] == _WORKSPACE_ID
        assert call.args[1] == _FEATURE_ID
        assert call.args[2] == "tasks.md"
        assert call.args[3] == _TASKS_MD
        assert result.get("branch") is None
        assert result.get("commit_sha") is None

    def test_tasks_stage_approve_runs_db_and_task_creation(self, monkeypatch):
        """Tasks-stage approve: DB update, then create/activate tasks."""
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)

        update_mock = AsyncMock()
        create_tasks_mock = MagicMock(return_value={"tasks": [{"id": "T1"}, {"id": "T2"}]})
        activate_mock = MagicMock(return_value=["T1"])
        read_content_mock = MagicMock(
            return_value={"content": _TASKS_MD, "version_id": "v1"}
        )

        mod = _load_approve_mod()
        mod.get_feature_detail = AsyncMock(
            return_value=_make_feature_detail(stages=_STAGES_TASKS_DRAFT)
        )
        mod.update_feature_stage = update_mock
        mod.read_document_content = read_content_mock
        mod._run_async_create_tasks = create_tasks_mock
        mod._activate_tasks_db = activate_mock

        result = mod.handle(
            stage="tasks",
            action="approve",
            workspace_id=_WORKSPACE_ID,
            feature_id=_FEATURE_ID,
        )

        assert result["ok"] is True, result.get("error")
        # tasks.md read from storage-service
        read_content_mock.assert_called_once()
        assert read_content_mock.call_args.args[2] == "tasks.md"
        # DB update
        update_mock.assert_called_once()
        update_call = update_mock.call_args[1]
        assert update_call["stage"] == "tasks"
        assert update_call["review_status"] == "approved"
        # Tasks created via API — parsed task list (not raw tasks.md)
        create_tasks_mock.assert_called_once()
        d_call = create_tasks_mock.call_args
        assert d_call.args[0] == _WORKSPACE_ID
        assert d_call.args[1] == _FEATURE_ID
        assert [t["name"] for t in d_call.args[2]] == ["T1", "T2"]
        assert result["commit_sha"] == ""
        assert result["branch"] is None

    def test_tasks_stage_approve_result_includes_activated_tasks(self, monkeypatch):
        """After the pipeline, the result reports activated tasks."""
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)

        activated = ["T1", "T2"]

        mod = _load_approve_mod()
        mod.get_feature_detail = AsyncMock(
            return_value=_make_feature_detail(stages=_STAGES_TASKS_DRAFT)
        )
        mod.update_feature_stage = AsyncMock()
        mod.read_document_content = MagicMock(
            return_value={"content": _TASKS_MD, "version_id": "v1"}
        )
        mod._run_async_create_tasks = MagicMock(return_value={"tasks": []})
        mod._activate_tasks_db = MagicMock(return_value=activated)

        result = mod.handle(
            stage="tasks",
            action="approve",
            workspace_id=_WORKSPACE_ID,
            feature_id=_FEATURE_ID,
        )

        assert result["ok"] is True, result.get("error")
        assert result.get("activated_tasks") == activated


# ---------------------------------------------------------------------------
# E2E Subtask 2 — Resumable approve: failure at b → re-run; failure at d → re-run
# ---------------------------------------------------------------------------


class TestE2EResumableApproveAfterDbUpdateFailure:
    """Simulate: first call fails at the DB status update; second call resumes
    and completes without duplication. go features have no git steps to skip
    on resume — DB update and task creation are both naturally idempotent.
    """

    def _run(
        self,
        monkeypatch,
        *,
        stages=None,
        update_feature_stage_raises=None,
        create_tasks_side_effect=None,
    ):
        """Run approve with src.services.workflow_backend_client stubbed so that
        the inline ``from ... import WorkflowBackendError`` inside
        approve.handle() resolves to our _WorkflowBackendError stand-in.
        """
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)

        if create_tasks_side_effect is None:
            create_tasks_side_effect = MagicMock(return_value={"tasks": []})

        update_mock = AsyncMock()
        if update_feature_stage_raises:
            update_mock.side_effect = update_feature_stage_raises

        from src.services.workflow_backend_client import run_async as _real_run_async

        wbc_stub = MagicMock()
        wbc_stub.WorkflowBackendError = _WorkflowBackendError
        wbc_stub.run_async = _real_run_async
        sys.modules["src.services.workflow_backend_client"] = wbc_stub

        mod = _load_approve_mod()
        mod.get_feature_detail = AsyncMock(
            return_value=_make_feature_detail(stages=stages or _STAGES_TASKS_DRAFT)
        )
        mod.update_feature_stage = update_mock
        mod.read_document_content = MagicMock(
            return_value={"content": _TASKS_MD, "version_id": "v1"}
        )
        mod._run_async_create_tasks = create_tasks_side_effect
        mod._activate_tasks_db = MagicMock(return_value=[])

        result = mod.handle(
            stage="tasks",
            action="approve",
            workspace_id=_WORKSPACE_ID,
            feature_id=_FEATURE_ID,
        )

        return result, update_mock, create_tasks_side_effect

    def test_first_call_fails_at_db_update(self, monkeypatch):
        result, _, _ = self._run(
            monkeypatch, update_feature_stage_raises=Exception("DB connection lost")
        )
        assert result["ok"] is False
        assert result.get("failed_step") == "c"

    def test_first_call_does_not_create_tasks_on_db_failure(self, monkeypatch):
        create_mock = MagicMock(return_value={"tasks": []})
        _, _, create_mock = self._run(
            monkeypatch,
            update_feature_stage_raises=Exception("DB error"),
            create_tasks_side_effect=create_mock,
        )
        create_mock.assert_not_called()

    def test_second_call_resumes_and_completes(self, monkeypatch):
        """Re-run after DB failure (stages now show approved): DB update and
        task creation both run again, idempotently, and succeed."""
        create_mock = MagicMock(return_value={"tasks": [{"id": "T1"}, {"id": "T2"}]})
        result, update_mock, _ = self._run(
            monkeypatch,
            stages=_STAGES_TASKS_APPROVED,
            create_tasks_side_effect=create_mock,
        )
        assert result["ok"] is True, result.get("error")
        assert "failed_step" not in result
        update_mock.assert_called_once()
        create_mock.assert_called_once()

    def test_second_call_tasks_already_exist_is_safe_no_op(self, monkeypatch):
        """If tasks were already created (by another path), it's a safe no-op."""
        already_exist = MagicMock(
            side_effect=_WorkflowBackendError(
                "tasks already exist",
                reason_code="tasks_already_exist",
                status=409,
            )
        )
        result, _, _ = self._run(
            monkeypatch,
            stages=_STAGES_TASKS_APPROVED,
            create_tasks_side_effect=already_exist,
        )
        assert result["ok"] is True
        assert result.get("failed_step") is None

    def test_second_call_no_duplication_when_tasks_exist(self, monkeypatch):
        """Re-run with tasks_already_exist: creation is attempted (not retried) once."""
        create_mock = MagicMock(
            side_effect=_WorkflowBackendError(
                "tasks already exist",
                reason_code="tasks_already_exist",
                status=409,
            )
        )
        result, _, _ = self._run(
            monkeypatch,
            stages=_STAGES_TASKS_APPROVED,
            create_tasks_side_effect=create_mock,
        )
        create_mock.assert_called_once()
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# E2E Subtask 3 — Backup /create-tasks: guard reject; success; tasks-exist no-op
# ---------------------------------------------------------------------------


def _run_create_tasks_handle(monkeypatch, *, create_mock, tasks_content=_TASKS_MD):
    """Run create_tasks.handle() with all dependencies mocked via sys.modules."""
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
