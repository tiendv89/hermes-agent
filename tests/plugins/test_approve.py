"""
Covers:
  - Helper functions: _find_open_prs, _merge_pr, _ensure_docs_on_base (ts-only
    helpers — retained for the git-based approval path, unused by go)
  - handle() for go + tasks + approve: full happy path (DB update, then
    create/activate tasks via storage-service-read tasks.md — no git at all)
  - handle() resumable: already-approved re-run still runs DB update + task
    creation/activation idempotently
  - handle() step c: DB update called with correct args
  - handle() step c failure → halted before task creation
  - handle() step d: tasks_already_exist → safe no-op (ok=True)
  - handle() step d: other reason code → error relayed to chat
  - handle() step d: missing/unreadable tasks.md → error
  - Earlier stages (product_spec, technical_design) for go owner: DB-only, unchanged
  - go features never touch git anywhere in the tasks-approve pipeline
  - ts feature: existing behavior unchanged (git commit, no DB pipeline)
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


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_TOKEN = "ghp_test"
_WORKSPACE_ID = "ws-test"
_FEATURE_ID = "my-feature"
_OWNER = "testorg"
_REPO = "testws"
_BASE_BRANCH = "main"
_FEATURE_BRANCH = "feature/my-feature"
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

_STATUS_SHA = "sha_status_123"
_TASKS_MD_SHA = "sha_tasks_456"
_COMMIT_SHA = "newcommit789"


def _make_workspace_context():
    return {
        "management_repo": _REPO,
        "repos": [{"id": _REPO, "github": f"https://github.com/{_OWNER}/{_REPO}"}],
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


def _make_read_document(
    status_content="", tasks_content=_TASKS_MD
):
    """Return a side_effect for read_document (ts-only git path) that dispatches by path suffix."""

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


# ---------------------------------------------------------------------------
# _find_open_prs / _merge_pr / _ensure_docs_on_base
#
# These are generic GitHub PR-merge helpers used by the ts/git approval path
# (via document_repo elsewhere). They're no longer called from the go pipeline
# (go features have no init PR to merge — workflow-backend stopped creating
# one), but remain in module for potential ts-side reuse and stay covered here
# as standalone units.
# ---------------------------------------------------------------------------


class TestFindOpenPrs:
    def test_calls_github_api_with_correct_params(self):
        mod = _load_approve_mod()

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [
            {"number": 1, "html_url": "https://github.com/x/y/pull/1"}
        ]

        with patch("requests.get", return_value=mock_resp) as mock_get:
            result = mod._find_open_prs(
                _OWNER, _REPO, _FEATURE_BRANCH, _BASE_BRANCH, _GITHUB_TOKEN
            )

        assert len(result) == 1
        assert result[0]["number"] == 1
        params = mock_get.call_args[1]["params"]
        assert params["state"] == "open"
        assert _FEATURE_BRANCH in params["head"]
        assert params["base"] == _BASE_BRANCH

    def test_returns_empty_list_when_no_prs(self):
        mod = _load_approve_mod()

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = []

        with patch("requests.get", return_value=mock_resp):
            result = mod._find_open_prs(
                _OWNER, _REPO, _FEATURE_BRANCH, _BASE_BRANCH, _GITHUB_TOKEN
            )

        assert result == []

    def test_raises_on_http_error(self):
        mod = _load_approve_mod()

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = Exception("403 Forbidden")

        with patch("requests.get", return_value=mock_resp):
            with pytest.raises(Exception, match="403"):
                mod._find_open_prs(
                    _OWNER, _REPO, _FEATURE_BRANCH, _BASE_BRANCH, _GITHUB_TOKEN
                )


class TestMergePr:
    def test_calls_github_merge_api(self):
        mod = _load_approve_mod()

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()

        with patch("requests.put", return_value=mock_resp) as mock_put:
            mod._merge_pr(_OWNER, _REPO, 42, _GITHUB_TOKEN)

        url = mock_put.call_args[0][0]
        assert "pulls/42/merge" in url

    def test_raises_on_405_not_mergeable(self):
        mod = _load_approve_mod()

        mock_resp = MagicMock()
        mock_resp.status_code = 405
        mock_resp.text = "Pull Request is not mergeable"

        with patch("requests.put", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="not mergeable"):
                mod._merge_pr(_OWNER, _REPO, 42, _GITHUB_TOKEN)

    def test_raises_on_409_conflict(self):
        mod = _load_approve_mod()

        mock_resp = MagicMock()
        mock_resp.status_code = 409
        mock_resp.text = "Merge conflict"

        with patch("requests.put", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="conflict"):
                mod._merge_pr(_OWNER, _REPO, 42, _GITHUB_TOKEN)


class TestEnsureDocsOnBase:
    def _run(self, base_status, open_prs, merge_raises=None):
        mod = _load_approve_mod()

        merge_mock = MagicMock()
        if merge_raises:
            merge_mock.side_effect = merge_raises

        with (
            patch.object(mod, "_read_status_yaml_on_branch", return_value=base_status),
            patch.object(mod, "_find_open_prs", return_value=open_prs),
            patch.object(mod, "_merge_pr", merge_mock),
        ):
            result = mod._ensure_docs_on_base(
                _OWNER,
                _REPO,
                _FEATURE_BRANCH,
                _BASE_BRANCH,
                f"docs/features/{_FEATURE_ID}/status.yaml",
                _GITHUB_TOKEN,
            )

        return result, merge_mock

    def test_skips_when_base_already_has_approved_docs(self):
        base_status = {"stages": {"tasks": {"review_status": "approved"}}}
        result, merge_mock = self._run(base_status=base_status, open_prs=[])
        assert result is None
        merge_mock.assert_not_called()

    def test_merges_single_open_pr(self):
        open_prs = [{"number": 5, "html_url": "https://github.com/x/y/pull/5"}]
        result, merge_mock = self._run(base_status=None, open_prs=open_prs)
        assert result is None
        merge_mock.assert_called_once_with(_OWNER, _REPO, 5, _GITHUB_TOKEN)

    def test_returns_error_on_zero_prs(self):
        result, _ = self._run(base_status=None, open_prs=[])
        assert result is not None
        assert "No open PR" in result
        assert _FEATURE_BRANCH in result
        assert _BASE_BRANCH in result

    def test_returns_error_on_multiple_prs(self):
        open_prs = [
            {"number": 3, "html_url": "https://github.com/x/y/pull/3"},
            {"number": 7, "html_url": "https://github.com/x/y/pull/7"},
        ]
        result, _ = self._run(base_status=None, open_prs=open_prs)
        assert result is not None
        assert "Multiple" in result
        assert "#3" in result
        assert "#7" in result

    def test_returns_error_on_merge_failure(self):
        open_prs = [{"number": 5, "html_url": "https://github.com/x/y/pull/5"}]
        result, _ = self._run(
            base_status=None, open_prs=open_prs, merge_raises=RuntimeError("conflict")
        )
        assert result is not None

    def test_error_includes_rerun_guidance(self):
        result, _ = self._run(base_status=None, open_prs=[])
        assert "re-run" in result.lower() or "Re-run" in result


# ---------------------------------------------------------------------------
# Shared helper for go-tasks-approve handle() tests
#
# go features track status via get_feature_detail's "stages" field (DB-backed)
# and read tasks.md from storage-service — no git access at all. read_document
# / commit_to_branch / _commit_files are stubbed to raise if called, so any
# accidental git touch fails the test loudly instead of silently mocking through.
# ---------------------------------------------------------------------------


def _run_go_tasks_approve_handle(
    monkeypatch,
    *,
    stages=None,
    tasks_content=_TASKS_MD,
    create_tasks_side_effect=None,
    commit_files_sha=_COMMIT_SHA,
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

    monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
    monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

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
    mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
    mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail(owner, stages))
    mod.update_feature_stage = update_mock
    mod.read_document_content = read_content_mock
    mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
    mod._run_async_create_tasks = create_mock
    mod._activate_tasks_db = activate_mock

    # go features must never touch git — fail loudly (not silently) if they do.
    mod.read_document = MagicMock(
        side_effect=AssertionError("git read_document must not be called for go features")
    )
    mod.commit_to_branch = MagicMock(
        side_effect=AssertionError("git commit_to_branch must not be called for go features")
    )

    with patch(
        "plugins.tools.tasks_write._commit_files",
        side_effect=AssertionError("git _commit_files must not be called for go features"),
    ):
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
            read_document_content_side_effect=RuntimeError("storage-service unavailable"),
        )
        assert result["ok"] is False
        assert result["failed_step"] == "d"


# ---------------------------------------------------------------------------
# handle() — earlier stages for go owner: DB-only (behavior unchanged)
# ---------------------------------------------------------------------------


class TestGoEarlierStagesDbOnly:
    def _run_handle(self, monkeypatch, *, stage, action="approve"):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

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
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(
            return_value=_make_feature_detail("go", stages)
        )
        mod.update_feature_stage = update_mock
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._run_async_create_tasks = create_mock
        # go features must never touch git.
        mod.read_document = MagicMock(
            side_effect=AssertionError("git must not be touched for go features")
        )
        mod.commit_to_branch = MagicMock(
            side_effect=AssertionError("git must not be touched for go features")
        )

        with patch(
            "plugins.tools.tasks_write._commit_files",
            side_effect=AssertionError("git must not be touched for go features"),
        ):
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


# ---------------------------------------------------------------------------
# handle() — ts feature: existing behavior unchanged (git commit, no pipeline)
# ---------------------------------------------------------------------------


class TestTsFeatureBehaviorUnchanged:
    def test_ts_tasks_approve_commits_to_git(self, monkeypatch):
        """ts feature tasks-stage approve: commits status.yaml to git (not DB pipeline)."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        status_yaml = """\
feature_id: my-feature
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

        commit_to_branch_mock = MagicMock(return_value="sha_ts")
        update_mock = AsyncMock()
        create_tasks_mock = MagicMock()
        activate_git_mock = MagicMock(
            return_value={"activated": ["T1"], "commit_sha": "sha_act"}
        )

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail("ts"))
        mod.update_feature_stage = update_mock
        mod.read_document = MagicMock(
            side_effect=_make_read_document(status_content=status_yaml)
        )
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._run_async_create_tasks = create_tasks_mock
        mod._activate_tasks_git = activate_git_mock
        mod.commit_to_branch = commit_to_branch_mock

        with (
            patch("plugins.document_repo.branch_exists", return_value=True),
            patch("plugins.tools.tasks_write._commit_files", MagicMock()),
        ):
            result = mod.handle(
                stage="tasks",
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        commit_to_branch_mock.assert_called_once()
        update_mock.assert_not_called()
        create_tasks_mock.assert_not_called()

    def test_ts_product_spec_approve_unchanged(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        status_yaml = """\
feature_id: my-feature
feature_status: in_design
current_stage: product_spec
stages:
  product_spec:
    review_status: draft
    reviewed_by: null
    reviewed_at: null
    review_comment: null
    review_history: []
history: []
"""
        commit_to_branch_mock = MagicMock(return_value="sha_ts_ps")
        update_mock = AsyncMock()

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail("ts"))
        mod.update_feature_stage = update_mock
        mod.read_document = MagicMock(
            return_value={"content": status_yaml, "sha": "s1"}
        )
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod.commit_to_branch = commit_to_branch_mock

        with (
            patch("plugins.document_repo.branch_exists", return_value=True),
            patch("plugins.tools.tasks_write._commit_files", MagicMock()),
        ):
            result = mod.handle(
                stage="product_spec",
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        commit_to_branch_mock.assert_called_once()
        update_mock.assert_not_called()
