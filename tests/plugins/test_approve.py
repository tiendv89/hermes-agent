"""
Covers:
  - handle() for tasks + approve: full happy path (DB update, then
    create/activate tasks via storage-service-read tasks.md)
  - handle() resumable: already-approved re-run still runs DB update + task
    creation/activation idempotently
  - handle() step c: DB update called with correct args
  - handle() step c failure → halted before task creation
  - handle() step d: tasks_already_exist → safe no-op (ok=True)
  - handle() step d: other reason code → error relayed to chat
  - handle() step d: missing/unreadable tasks.md → error
  - Earlier stages (product_spec, technical_design): DB-only, unchanged
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_WORKSPACE_ID = "ws-test"
_FEATURE_ID = "my-feature"
_ACTOR = "agent@example.com"

_TASKS_MD = """\
# Tasks

## Index

| ID | Title | Repo | Depends On | Actor |
|----|-------|------|------------|-------|
| T1 | First task | hermes-agent | — | agent |
| T2 | Second task | hermes-agent | T1 | agent |

## T1 — First task
"""

# Per-stage "stages" JSONB, as returned by get_feature_detail for a go feature
# in tasks stage awaiting approval (mirrors workflow-backend's workspace_features
# table — see migrations/00003_workspace_features.sql in workflow-backend).
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
        "reviewed_by": _ACTOR,
        "reviewed_at": "2026-07-03T12:00:00+0000",
        "review_comment": None,
        "review_history": [
            {
                "review_status": "approved",
                "reviewed_by": _ACTOR,
                "reviewed_at": "2026-07-03T12:00:00+0000",
                "comment": None,
            }
        ],
    }
}

def _make_feature_detail(owner="go", stages=None):
    return {
        "feature_name": _FEATURE_ID,
        "title": "My Feature",
        "stage": "tasks",
        "status": "in_tdd",
        "next_action": "Awaiting tasks approval.",
        "owner": owner,
        "init_pr_url": None,
        "stages": stages if stages is not None else {},
    }


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


# ---------------------------------------------------------------------------
# Shared helper for tasks-approve handle() tests
#
# Features track status via get_feature_detail's "stages" field (DB-backed)
# and read tasks.md from storage-service.
# ---------------------------------------------------------------------------


def _run_go_tasks_approve_handle(
    monkeypatch,
    *,
    stages=None,
    tasks_content=_TASKS_MD,
    create_tasks_side_effect=None,
    activated_tasks=None,
    update_feature_stage_raises=None,
    owner="go",
    read_document_content_side_effect=None,
):
    """Run handle(stage='tasks', action='approve') with all external calls mocked."""
    if stages is None:
        stages = _STAGES_TASKS_DRAFT
    if activated_tasks is None:
        activated_tasks = ["T1"]

    monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)

    update_mock = AsyncMock()
    if update_feature_stage_raises:
        update_mock.side_effect = update_feature_stage_raises

    create_mock = MagicMock(return_value={"tasks": []})
    if create_tasks_side_effect is not None:
        create_mock.side_effect = create_tasks_side_effect

    activate_mock = MagicMock(return_value=activated_tasks)

    if read_document_content_side_effect is not None:
        read_content_mock = MagicMock(side_effect=read_document_content_side_effect)
    else:
        read_content_mock = MagicMock(
            return_value={"content": tasks_content, "version_id": "v1"}
        )

    mod = _load_approve_mod()

    # Patch module-level imports in approve's namespace
    mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail(owner, stages))
    mod.update_feature_stage = update_mock
    mod.read_document_content = read_content_mock
    mod._run_async_create_tasks = create_mock
    mod._activate_tasks_db = activate_mock

    result = mod.handle(
        stage="tasks",
        action="approve",
        workspace_id=_WORKSPACE_ID,
        feature_id=_FEATURE_ID,
    )

    return result, {
        "update_stage": update_mock,
        "create_tasks": create_mock,
        "activate": activate_mock,
        "read_document_content": read_content_mock,
    }


# ---------------------------------------------------------------------------
# handle() — go + tasks + approve: full happy path
# ---------------------------------------------------------------------------


class TestGoTasksApprovePipelineHappyPath:
    def test_happy_path_returns_ok_true(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(monkeypatch)
        assert result["ok"] is True, result.get("error")

    def test_happy_path_reads_tasks_md_from_storage_service(self, monkeypatch):
        _, mocks = _run_go_tasks_approve_handle(monkeypatch)
        mocks["read_document_content"].assert_called_once()
        call = mocks["read_document_content"].call_args
        assert call.args[0] == _WORKSPACE_ID
        assert call.args[1] == _FEATURE_ID
        assert call.args[2] == "tasks.md"

    def test_happy_path_updates_db_step_c(self, monkeypatch):
        _, mocks = _run_go_tasks_approve_handle(monkeypatch)
        assert mocks["update_stage"].called
        call_kwargs = mocks["update_stage"].call_args[1]
        assert call_kwargs["stage"] == "tasks"
        assert call_kwargs["review_status"] == "approved"
        assert call_kwargs["feature_status"] == "ready_for_implementation"

    def test_happy_path_creates_tasks_step_d(self, monkeypatch):
        _, mocks = _run_go_tasks_approve_handle(monkeypatch)
        mocks["create_tasks"].assert_called_once()
        call = mocks["create_tasks"].call_args
        assert call.args[0] == _WORKSPACE_ID
        assert call.args[1] == _FEATURE_ID
        # Step d passes the parsed task list (not the raw tasks.md).
        assert [t["name"] for t in call.args[2]] == ["T1", "T2"]

    def test_happy_path_activates_tasks(self, monkeypatch):
        result, mocks = _run_go_tasks_approve_handle(monkeypatch)
        assert mocks["activate"].called
        assert result["activated_tasks"] == ["T1"]

    def test_no_commit_sha_in_result(self, monkeypatch):
        """go features never git-commit — commit_sha stays empty."""
        result, _ = _run_go_tasks_approve_handle(monkeypatch)
        assert result["commit_sha"] == ""

    def test_branch_is_none_in_result(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(monkeypatch)
        assert result["branch"] is None

    def test_review_status_approved_in_result(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(monkeypatch)
        assert result["review_status"] == "approved"

    def test_feature_status_advanced(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(monkeypatch)
        assert result["feature_status"] == "ready_for_implementation"

    def test_current_stage_advanced_to_handoff(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(monkeypatch)
        assert result["current_stage"] == "handoff"


# ---------------------------------------------------------------------------
# handle() — resumable: re-running an already-approved tasks stage
#
# Steps c (DB update) and d (create/activate tasks) still run — c is an
# idempotent set (not an append) and d's tasks_already_exist reason code is
# treated as a no-op (see TestGoTasksApproveStepDTasksAlreadyExist).
# ---------------------------------------------------------------------------


class TestGoTasksApproveResumable:
    def test_already_approved_still_runs_step_c(self, monkeypatch):
        _, mocks = _run_go_tasks_approve_handle(
            monkeypatch, stages=_STAGES_TASKS_APPROVED
        )
        mocks["update_stage"].assert_called_once()

    def test_already_approved_still_runs_step_d(self, monkeypatch):
        _, mocks = _run_go_tasks_approve_handle(
            monkeypatch, stages=_STAGES_TASKS_APPROVED
        )
        mocks["create_tasks"].assert_called_once()

    def test_already_approved_returns_ok(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, stages=_STAGES_TASKS_APPROVED
        )
        assert result["ok"] is True


# ---------------------------------------------------------------------------
# handle() — step c (DB status update) failure
# ---------------------------------------------------------------------------


class TestGoTasksApproveStepCFailure:
    def test_db_update_failure_returns_step_c_error(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch,
            update_feature_stage_raises=Exception("DB connection lost"),
        )
        assert result["ok"] is False
        assert result["failed_step"] == "c"
        assert "Step c" in result["error"] or "step c" in result["error"].lower()

    def test_step_d_not_called_on_step_c_failure(self, monkeypatch):
        result, mocks = _run_go_tasks_approve_handle(
            monkeypatch,
            update_feature_stage_raises=Exception("DB error"),
        )
        mocks["create_tasks"].assert_not_called()


# ---------------------------------------------------------------------------
# handle() — step d: tasks_already_exist is a safe no-op
# ---------------------------------------------------------------------------


class TestGoTasksApproveStepDTasksAlreadyExist:
    def test_tasks_already_exist_is_ok(self, monkeypatch):
        # Patch WorkflowBackendError in place on the real module — approve.py
        # also imports get_feature_detail/get_workspace_context/run_async/etc.
        # from it, so replacing it wholesale would break those imports.
        import src.services.workflow_backend_client as wbe_mod

        class _FakeWBE(Exception):
            def __init__(self, msg="", *, reason_code="", status=0):
                super().__init__(msg)
                self.reason_code = reason_code
                self.status = status

        wbe_mod.WorkflowBackendError = _FakeWBE

        exc = _FakeWBE("tasks already exist", reason_code="tasks_already_exist")
        result, mocks = _run_go_tasks_approve_handle(
            monkeypatch, create_tasks_side_effect=exc
        )
        assert result["ok"] is True
        # Activation still runs after tasks_already_exist no-op
        mocks["activate"].assert_called()

    def test_tasks_already_exist_does_not_return_failed_step(self, monkeypatch):
        import src.services.workflow_backend_client as wbe_mod

        class _FakeWBE(Exception):
            def __init__(self, msg="", *, reason_code="", status=0):
                super().__init__(msg)
                self.reason_code = reason_code
                self.status = status

        wbe_mod.WorkflowBackendError = _FakeWBE

        exc = _FakeWBE("tasks already exist", reason_code="tasks_already_exist")
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, create_tasks_side_effect=exc
        )
        assert "failed_step" not in result


# ---------------------------------------------------------------------------
# handle() — step d: other reason codes relayed to chat
# ---------------------------------------------------------------------------


class TestGoTasksApproveStepDReasonCodeRelay:
    def _setup_fake_wbe(self, reason_code):
        import src.services.workflow_backend_client as wbe_mod

        class _FakeWBE(Exception):
            def __init__(self, msg="", *, reason_code="", status=0):
                super().__init__(msg)
                self.reason_code = reason_code
                self.status = status

        wbe_mod.WorkflowBackendError = _FakeWBE
        return _FakeWBE("error", reason_code=reason_code)

    def test_feature_not_tasks_approved_relayed(self, monkeypatch):
        exc = self._setup_fake_wbe("feature_not_tasks_approved")
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, create_tasks_side_effect=exc
        )
        assert result["ok"] is False
        assert result["failed_step"] == "d"
        assert result["reason_code"] == "feature_not_tasks_approved"

    def test_missing_config_relayed(self, monkeypatch):
        exc = self._setup_fake_wbe("missing_config")
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, create_tasks_side_effect=exc
        )
        assert result["ok"] is False
        assert result["reason_code"] == "missing_config"

    def test_step_d_named_in_error(self, monkeypatch):
        exc = self._setup_fake_wbe("feature_not_tasks_approved")
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, create_tasks_side_effect=exc
        )
        assert "Step d" in result["error"] or "step d" in result["error"].lower()

    def test_generic_exception_in_step_d(self, monkeypatch):
        exc = RuntimeError("network timeout")
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, create_tasks_side_effect=exc
        )
        assert result["ok"] is False
        assert result["failed_step"] == "d"


# ---------------------------------------------------------------------------
# handle() — step d: missing / unreadable tasks.md
# ---------------------------------------------------------------------------


class TestGoTasksApproveStepDMissingTasksMd:
    def test_missing_tasks_md_returns_step_d_error(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(monkeypatch, tasks_content="")
        assert result["ok"] is False
        assert result["failed_step"] == "d"
        assert "tasks.md" in result["error"]

    def test_storage_service_read_error_returns_step_d_error(self, monkeypatch):
        """A storage-service error reading tasks.md is a step-d error, not a crash."""
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch,
            read_document_content_side_effect=RuntimeError(
                "storage-service unavailable"
            ),
        )
        assert result["ok"] is False
        assert result["failed_step"] == "d"


# ---------------------------------------------------------------------------
# handle() — earlier stages: DB-only
# ---------------------------------------------------------------------------


class TestGoEarlierStagesDbOnly:
    def _run_handle(self, monkeypatch, *, stage, action="approve"):
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)

        stages = {
            stage: {
                "review_status": "draft",
                "reviewed_by": None,
                "reviewed_at": None,
                "review_comment": None,
                "review_history": [],
            }
        }
        update_mock = AsyncMock()
        create_mock = MagicMock()

        mod = _load_approve_mod()
        mod.get_feature_detail = AsyncMock(
            return_value=_make_feature_detail("go", stages)
        )
        mod.update_feature_stage = update_mock
        mod._run_async_create_tasks = create_mock

        result = mod.handle(
            stage=stage,
            action=action,
            workspace_id=_WORKSPACE_ID,
            feature_id=_FEATURE_ID,
        )

        return result, {"update_stage": update_mock, "create_tasks": create_mock}

    def test_product_spec_approve_uses_db_only(self, monkeypatch):
        result, mocks = self._run_handle(monkeypatch, stage="product_spec")
        assert result["ok"] is True, result.get("error")
        assert mocks["update_stage"].called
        mocks["create_tasks"].assert_not_called()

    def test_technical_design_approve_uses_db_only(self, monkeypatch):
        result, mocks = self._run_handle(monkeypatch, stage="technical_design")
        assert result["ok"] is True, result.get("error")
        assert mocks["update_stage"].called
        mocks["create_tasks"].assert_not_called()

    def test_tasks_reject_uses_db_only(self, monkeypatch):
        """tasks + reject for go: DB update only, no pipeline."""
        result, mocks = self._run_handle(monkeypatch, stage="tasks", action="reject")
        assert result["ok"] is True, result.get("error")
        assert mocks["update_stage"].called
        mocks["create_tasks"].assert_not_called()
