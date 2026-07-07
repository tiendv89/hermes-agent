"""
Covers:
  - Helper functions: _find_open_prs, _merge_pr, _ensure_docs_on_base
  - handle() for go + tasks + approve: full happy path (a→b→c→d)
  - handle() resumable: already_approved=True → step a skipped, b→c→d run
  - handle() step b: already-on-base skip (base branch has approved docs)
  - handle() step b: single open PR → merge
  - handle() step b: zero open PRs → halt with message
  - handle() step b: multiple open PRs → halt with message
  - handle() step b: PR merge failure → halt with message
  - handle() step c: DB update called with correct args
  - handle() step d: tasks_already_exist → safe no-op (ok=True)
  - handle() step d: other reason code → error relayed to chat
  - handle() step d: missing tasks.md → error
  - handle() step d: missing_config → error with guidance
  - handle() step a failure → error names step a
  - Earlier stages (product_spec, technical_design) for go owner: DB-only, unchanged
  - ts feature: existing behavior unchanged (git commit, no pipeline)
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
    monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
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

# A status.yaml for a go feature in tasks stage (awaiting approval)
_STATUS_YAML_TASKS_DRAFT = """\
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
revalidation:
  product_spec_required: false
  technical_design_required: false
  tasks_required: false
  deployment_checklist_required: false
"""

# A status.yaml that already shows tasks approved (for resume testing)
_STATUS_YAML_TASKS_APPROVED = """\
feature_id: my-feature
feature_status: ready_for_implementation
current_stage: handoff
next_action: Tasks ready for implementation.
stages:
  tasks:
    review_status: approved
    reviewed_by: agent@example.com
    reviewed_at: 2026-07-03T12:00:00+0000
    review_comment: null
    review_history:
      - review_status: approved
        reviewed_by: agent@example.com
        reviewed_at: 2026-07-03T12:00:00+0000
history: []
"""

_STATUS_SHA = "sha_status_123"
_TASKS_MD_SHA = "sha_tasks_456"
_COMMIT_SHA = "newcommit789"


def _make_workspace_context():
    return {
        "management_repo": _REPO,
        "repos": [{"id": _REPO, "github": f"https://github.com/{_OWNER}/{_REPO}"}],
    }


def _make_feature_detail(owner="go"):
    return {
        "feature_name": _FEATURE_ID,
        "title": "My Feature",
        "stage": "tasks",
        "status": "in_tdd",
        "next_action": "Awaiting tasks approval.",
        "owner": owner,
        "init_pr_url": None,
    }


def _make_read_document(
    status_content=_STATUS_YAML_TASKS_DRAFT, tasks_content=_TASKS_MD
):
    """Return a side_effect for read_document that dispatches by path suffix."""

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
# _find_open_prs
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


# ---------------------------------------------------------------------------
# _merge_pr
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _ensure_docs_on_base
# ---------------------------------------------------------------------------


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
# Shared helper for handle() tests
# ---------------------------------------------------------------------------


def _run_go_tasks_approve_handle(
    monkeypatch,
    *,
    status_content=_STATUS_YAML_TASKS_DRAFT,
    tasks_content=_TASKS_MD,
    base_status=None,
    open_prs=None,
    merge_raises=None,
    create_tasks_side_effect=None,
    commit_files_sha=_COMMIT_SHA,
    activated_tasks=None,
    update_feature_stage_raises=None,
    owner="go",
    read_document_side_effect=None,
):
    """Run handle(stage='tasks', action='approve') with all external calls mocked.

    Patches are applied directly on the loaded module (correct for module-level imports).
    """
    if open_prs is None:
        open_prs = [{"number": 1, "html_url": "https://github.com/x/y/pull/1"}]
    if activated_tasks is None:
        activated_tasks = ["T1"]

    monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
    monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

    merge_mock = MagicMock()
    if merge_raises:
        merge_mock.side_effect = merge_raises

    update_mock = AsyncMock()
    if update_feature_stage_raises:
        update_mock.side_effect = update_feature_stage_raises

    create_mock = MagicMock(return_value={"tasks": []})
    if create_tasks_side_effect is not None:
        create_mock.side_effect = create_tasks_side_effect

    commit_mock = MagicMock(return_value=commit_files_sha)
    activate_mock = MagicMock(return_value=activated_tasks)

    mod = _load_approve_mod()

    # Patch module-level imports in approve's namespace
    mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
    mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail(owner))
    mod.update_feature_stage = update_mock
    mod.read_document = MagicMock(
        side_effect=read_document_side_effect
        or _make_read_document(status_content, tasks_content)
    )
    mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
    mod._read_status_yaml_on_branch = MagicMock(return_value=base_status)
    mod._find_open_prs = MagicMock(return_value=open_prs)
    mod._merge_pr = merge_mock
    mod._run_async_create_tasks = create_mock
    mod._activate_tasks_db = activate_mock

    # branch_exists is an inline import; patch via module
    with patch("plugins.document_repo.branch_exists", return_value=False):
        # _commit_files is imported inline inside handle()
        with patch("plugins.tools.tasks_write._commit_files", commit_mock):
            # commit_to_branch is a top-level import; patch via module
            mod.commit_to_branch = MagicMock(return_value=_COMMIT_SHA)
            result = mod.handle(
                stage="tasks",
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

    return result, {
        "commit": commit_mock,
        "merge": merge_mock,
        "update_stage": update_mock,
        "create_tasks": create_mock,
        "activate": activate_mock,
    }


# ---------------------------------------------------------------------------
# handle() — go + tasks + approve: full happy path
# ---------------------------------------------------------------------------


class TestGoTasksApprovePipelineHappyPath:
    def test_happy_path_returns_ok_true(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(monkeypatch)
        assert result["ok"] is True

    def test_happy_path_commits_status_yaml_step_a(self, monkeypatch):
        _, mocks = _run_go_tasks_approve_handle(monkeypatch)
        assert mocks["commit"].called
        files_arg = mocks["commit"].call_args[0][3]
        assert any("status.yaml" in p for p in files_arg)

    def test_happy_path_merges_pr_step_b(self, monkeypatch):
        _, mocks = _run_go_tasks_approve_handle(monkeypatch)
        mocks["merge"].assert_called_once_with(_OWNER, _REPO, 1, _GITHUB_TOKEN)

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

    def test_commit_sha_in_result(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(monkeypatch)
        assert result["commit_sha"] == _COMMIT_SHA

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
# handle() — resumable: already_approved=True → step a skipped
# ---------------------------------------------------------------------------


class TestGoTasksApproveResumable:
    def test_already_approved_skips_step_a(self, monkeypatch):
        """When status.yaml already shows approved, step a git commit is skipped."""
        _, mocks = _run_go_tasks_approve_handle(
            monkeypatch, status_content=_STATUS_YAML_TASKS_APPROVED
        )
        mocks["commit"].assert_not_called()

    def test_already_approved_still_runs_step_b(self, monkeypatch):
        """Step b still runs even when step a is skipped."""
        open_prs = [{"number": 3, "html_url": "https://github.com/x/y/pull/3"}]
        _, mocks = _run_go_tasks_approve_handle(
            monkeypatch,
            status_content=_STATUS_YAML_TASKS_APPROVED,
            open_prs=open_prs,
        )
        mocks["merge"].assert_called_once()

    def test_already_approved_still_runs_step_c(self, monkeypatch):
        """Step c still runs even when step a is skipped."""
        _, mocks = _run_go_tasks_approve_handle(
            monkeypatch, status_content=_STATUS_YAML_TASKS_APPROVED
        )
        mocks["update_stage"].assert_called_once()

    def test_already_approved_still_runs_step_d(self, monkeypatch):
        """Step d still runs even when step a is skipped."""
        _, mocks = _run_go_tasks_approve_handle(
            monkeypatch, status_content=_STATUS_YAML_TASKS_APPROVED
        )
        mocks["create_tasks"].assert_called_once()

    def test_already_approved_returns_ok(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, status_content=_STATUS_YAML_TASKS_APPROVED
        )
        assert result["ok"] is True

    def test_already_approved_step_b_skips_when_base_has_docs(self, monkeypatch):
        """If base already has approved docs, step b skips and does not merge."""
        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)
        _, mocks = _run_go_tasks_approve_handle(
            monkeypatch,
            status_content=_STATUS_YAML_TASKS_APPROVED,
            base_status=base_status,
            open_prs=[],
        )
        mocks["merge"].assert_not_called()

    def test_resume_after_step_b_done_creates_tasks(self, monkeypatch):
        """If step b is already done (base has docs), step d still runs."""
        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)
        _, mocks = _run_go_tasks_approve_handle(
            monkeypatch,
            status_content=_STATUS_YAML_TASKS_APPROVED,
            base_status=base_status,
        )
        mocks["create_tasks"].assert_called_once()


# ---------------------------------------------------------------------------
# handle() — step b failure cases
# ---------------------------------------------------------------------------


class TestGoTasksApproveStepBFailures:
    def test_zero_prs_returns_error_with_guidance(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(monkeypatch, open_prs=[])
        assert result["ok"] is False
        assert result["failed_step"] == "b"
        assert "No open PR" in result["error"]
        assert "re-run" in result["error"].lower() or "Re-run" in result["error"]

    def test_multiple_prs_returns_error(self, monkeypatch):
        open_prs = [
            {"number": 5, "html_url": "https://github.com/x/y/pull/5"},
            {"number": 9, "html_url": "https://github.com/x/y/pull/9"},
        ]
        result, _ = _run_go_tasks_approve_handle(monkeypatch, open_prs=open_prs)
        assert result["ok"] is False
        assert result["failed_step"] == "b"
        assert "#5" in result["error"]
        assert "#9" in result["error"]

    def test_pr_merge_failure_returns_error(self, monkeypatch):
        open_prs = [{"number": 7, "html_url": "https://github.com/x/y/pull/7"}]
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch,
            open_prs=open_prs,
            merge_raises=RuntimeError("merge conflict"),
        )
        assert result["ok"] is False
        assert result["failed_step"] == "b"

    def test_step_b_named_in_error(self, monkeypatch):
        result, _ = _run_go_tasks_approve_handle(monkeypatch, open_prs=[])
        assert "Step b" in result["error"] or "step b" in result["error"].lower()

    def test_step_c_not_called_on_step_b_failure(self, monkeypatch):
        """If step b fails, steps c and d must not run (fail-fast)."""
        result, mocks = _run_go_tasks_approve_handle(monkeypatch, open_prs=[])
        mocks["update_stage"].assert_not_called()
        mocks["create_tasks"].assert_not_called()


# ---------------------------------------------------------------------------
# handle() — step c failure
# ---------------------------------------------------------------------------


class TestGoTasksApproveStepCFailure:
    def test_db_update_failure_returns_step_c_error(self, monkeypatch):
        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch,
            base_status=base_status,
            update_feature_stage_raises=Exception("DB connection lost"),
        )
        assert result["ok"] is False
        assert result["failed_step"] == "c"
        assert "Step c" in result["error"] or "step c" in result["error"].lower()

    def test_step_d_not_called_on_step_c_failure(self, monkeypatch):
        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)
        result, mocks = _run_go_tasks_approve_handle(
            monkeypatch,
            base_status=base_status,
            update_feature_stage_raises=Exception("DB error"),
        )
        mocks["create_tasks"].assert_not_called()


# ---------------------------------------------------------------------------
# handle() — step d: tasks_already_exist is a safe no-op
# ---------------------------------------------------------------------------


class TestGoTasksApproveStepDTasksAlreadyExist:
    def test_tasks_already_exist_is_ok(self, monkeypatch):
        """tasks_already_exist is treated as a no-op (idempotent)."""
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

        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)

        exc = _FakeWBE("tasks already exist", reason_code="tasks_already_exist")
        result, mocks = _run_go_tasks_approve_handle(
            monkeypatch,
            base_status=base_status,
            create_tasks_side_effect=exc,
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

        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)

        exc = _FakeWBE("tasks already exist", reason_code="tasks_already_exist")
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch,
            base_status=base_status,
            create_tasks_side_effect=exc,
        )
        assert "failed_step" not in result


# ---------------------------------------------------------------------------
# handle() — step d: other reason codes relayed to chat
# ---------------------------------------------------------------------------


class TestGoTasksApproveStepDReasonCodeRelay:
    def _setup_fake_wbe(self, reason_code):
        # Patch WorkflowBackendError in place on the real module (imported
        # for real, not stubbed) — approve.py/artifacts.py also import
        # get_feature_detail/get_workspace_context/run_async/etc. from this
        # same module, so replacing it wholesale would break those imports.
        import src.services.workflow_backend_client as wbe_mod

        class _FakeWBE(Exception):
            def __init__(self, msg="", *, reason_code="", status=0):
                super().__init__(msg)
                self.reason_code = reason_code
                self.status = status

        wbe_mod.WorkflowBackendError = _FakeWBE
        return _FakeWBE("error", reason_code=reason_code)

    def test_feature_not_tasks_approved_relayed(self, monkeypatch):
        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)
        exc = self._setup_fake_wbe("feature_not_tasks_approved")
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, base_status=base_status, create_tasks_side_effect=exc
        )
        assert result["ok"] is False
        assert result["failed_step"] == "d"
        assert result["reason_code"] == "feature_not_tasks_approved"

    def test_missing_config_relayed(self, monkeypatch):
        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)
        exc = self._setup_fake_wbe("missing_config")
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, base_status=base_status, create_tasks_side_effect=exc
        )
        assert result["ok"] is False
        assert result["reason_code"] == "missing_config"

    def test_step_d_named_in_error(self, monkeypatch):
        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)
        exc = self._setup_fake_wbe("feature_not_tasks_approved")
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, base_status=base_status, create_tasks_side_effect=exc
        )
        assert "Step d" in result["error"] or "step d" in result["error"].lower()

    def test_generic_exception_in_step_d(self, monkeypatch):
        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)
        exc = RuntimeError("network timeout")
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch, base_status=base_status, create_tasks_side_effect=exc
        )
        assert result["ok"] is False
        assert result["failed_step"] == "d"


# ---------------------------------------------------------------------------
# handle() — step d: missing tasks.md
# ---------------------------------------------------------------------------


class TestGoTasksApproveStepDMissingTasksMd:
    def test_missing_tasks_md_returns_step_d_error(self, monkeypatch):
        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)
        result, _ = _run_go_tasks_approve_handle(
            monkeypatch,
            base_status=base_status,
            tasks_content="",
        )
        assert result["ok"] is False
        assert result["failed_step"] == "d"
        assert "tasks.md" in result["error"]

    def test_reads_tasks_md_from_base_after_branch_auto_deleted(self, monkeypatch):
        """Step b merges the docs PR and GitHub auto-deletes the feature branch.

        Step d must read tasks.md from base_branch, not the now-deleted branch.
        """
        import yaml

        base_status = yaml.safe_load(_STATUS_YAML_TASKS_APPROVED)

        def _read_doc(gh_owner, gh_repo, branch, path, github_token):
            if path.endswith("tasks.md"):
                if branch == _BASE_BRANCH:
                    return {"content": _TASKS_MD, "sha": _TASKS_MD_SHA}
                # Feature branch was auto-deleted after the step-b merge.
                raise RuntimeError("404 Not Found: branch was deleted")
            if path.endswith("status.yaml"):
                return {"content": _STATUS_YAML_TASKS_DRAFT, "sha": _STATUS_SHA}
            return {"content": "", "sha": None}

        result, mocks = _run_go_tasks_approve_handle(
            monkeypatch,
            base_status=base_status,
            read_document_side_effect=_read_doc,
        )
        assert result["ok"] is True, result.get("error")
        # Tasks were created from the base-branch tasks.md despite the branch being gone.
        mocks["create_tasks"].assert_called_once()
        assert [t["name"] for t in mocks["create_tasks"].call_args.args[2]] == ["T1", "T2"]


# ---------------------------------------------------------------------------
# handle() — step a failure
# ---------------------------------------------------------------------------


class TestGoTasksApproveStepAFailure:
    def test_step_a_commit_failure_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail("go"))
        mod.update_feature_stage = AsyncMock()
        mod.read_document = MagicMock(side_effect=_make_read_document())
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._read_status_yaml_on_branch = MagicMock(return_value=None)
        mod._find_open_prs = MagicMock(return_value=[])
        mod._merge_pr = MagicMock()
        mod._run_async_create_tasks = MagicMock()
        mod._activate_tasks_db = MagicMock(return_value=[])
        mod.commit_to_branch = MagicMock()

        with (
            patch("plugins.document_repo.branch_exists", return_value=False),
            patch(
                "plugins.tools.tasks_write._commit_files",
                side_effect=Exception("git push rejected"),
            ),
        ):
            result = mod.handle(
                stage="tasks",
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert result["failed_step"] == "a"
        assert "Step a" in result["error"] or "step a" in result["error"].lower()

    def test_step_b_not_called_on_step_a_failure(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail("go"))
        mod.update_feature_stage = AsyncMock()
        mod.read_document = MagicMock(side_effect=_make_read_document())
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        find_prs_mock = MagicMock(return_value=[])
        mod._find_open_prs = find_prs_mock
        mod._merge_pr = MagicMock()
        mod._run_async_create_tasks = MagicMock()
        mod._activate_tasks_db = MagicMock(return_value=[])
        mod.commit_to_branch = MagicMock()

        with (
            patch("plugins.document_repo.branch_exists", return_value=False),
            patch(
                "plugins.tools.tasks_write._commit_files",
                side_effect=Exception("git error"),
            ),
        ):
            mod.handle(
                stage="tasks",
                action="approve",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        # Step b should NOT run since step a failed (fail-fast)
        find_prs_mock.assert_not_called()


# ---------------------------------------------------------------------------
# handle() — earlier stages for go owner: DB-only (behavior unchanged)
# ---------------------------------------------------------------------------


class TestGoEarlierStagesDbOnly:
    def _run_handle(self, monkeypatch, *, stage, action="approve"):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", _ACTOR)
        monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

        status_yaml = f"""\
feature_id: my-feature
feature_status: in_design
current_stage: {stage}
stages:
  {stage}:
    review_status: draft
    reviewed_by: null
    reviewed_at: null
    review_comment: null
    review_history: []
history: []
"""
        update_mock = AsyncMock()
        commit_mock = MagicMock()
        create_mock = MagicMock()

        mod = _load_approve_mod()
        mod.get_workspace_context = AsyncMock(return_value=_make_workspace_context())
        mod.get_feature_detail = AsyncMock(return_value=_make_feature_detail("go"))
        mod.update_feature_stage = update_mock
        mod.read_document = MagicMock(
            return_value={"content": status_yaml, "sha": "s1"}
        )
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._run_async_create_tasks = create_mock
        mod.commit_to_branch = MagicMock()

        with (
            patch("plugins.document_repo.branch_exists", return_value=False),
            patch("plugins.tools.tasks_write._commit_files", commit_mock),
        ):
            result = mod.handle(
                stage=stage,
                action=action,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        return result, {
            "commit": commit_mock,
            "update_stage": update_mock,
            "create_tasks": create_mock,
        }

    def test_product_spec_approve_uses_db_only(self, monkeypatch):
        result, mocks = self._run_handle(monkeypatch, stage="product_spec")
        assert result["ok"] is True
        assert mocks["update_stage"].called
        mocks["commit"].assert_not_called()
        mocks["create_tasks"].assert_not_called()

    def test_technical_design_approve_uses_db_only(self, monkeypatch):
        result, mocks = self._run_handle(monkeypatch, stage="technical_design")
        assert result["ok"] is True
        assert mocks["update_stage"].called
        mocks["commit"].assert_not_called()
        mocks["create_tasks"].assert_not_called()

    def test_tasks_reject_uses_db_only(self, monkeypatch):
        """tasks + reject for go: DB update only, no pipeline."""
        result, mocks = self._run_handle(monkeypatch, stage="tasks", action="reject")
        assert result["ok"] is True
        assert mocks["update_stage"].called
        mocks["commit"].assert_not_called()
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
        mod.read_document = MagicMock(side_effect=_make_read_document())
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
