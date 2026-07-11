"""
Covers:
  - _relay_create_tasks_reason_code: feature_not_tasks_approved, tasks_already_exist,
    missing_config, empty_tasks, unknown code
  - handle(): missing context → error
  - handle(): missing GITHUB_TOKEN → error
  - handle(): management repo resolution failure → error
  - handle(): tasks.md read failure → error
  - handle(): tasks.md missing (empty content) → error
  - handle(): success path → ok=True with "Tasks created successfully"
  - handle(): tasks_already_exist → ok=True noop message (safe no-op)
  - handle(): feature_not_tasks_approved → ok=False with approve-command guidance
  - handle(): missing_config → ok=False with config guidance
  - handle(): other WorkflowBackendError → ok=False with fallback message
  - handle(): generic exception → ok=False
  - handle(): workspace_id/feature_id override params respected
  - create_tasks registered in _TOOLS
  - create_tasks in register()
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

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

_GITHUB_TOKEN = "ghp_test"
_WORKSPACE_ID = "ws-test"
_FEATURE_ID = "my-feature"
_OWNER = "testorg"
_REPO = "testws"
_BASE_BRANCH = "main"
_FEATURE_BRANCH = "feature/my-feature"

_TASKS_MD = """\
# Tasks

## Index

| ID | Title | Repo | Depends On | Actor |
|----|-------|------|------------|-------|
| T1 | First task | hermes-agent | — | agent |
| T2 | Second task | hermes-agent | T1 | agent |

## T1 — First task
"""


def _make_workspace_context():
    return {
        "management_repo": _REPO,
        "repos": [{"id": _REPO, "github": f"https://github.com/{_OWNER}/{_REPO}"}],
    }


def _make_feature_detail():
    return {
        "feature_name": _FEATURE_ID,
        "title": "My Feature",
        "stage": "tasks",
        "status": "ready_for_implementation",
        "owner": "go",
        "init_pr_url": None,
    }


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


class _WorkflowBackendError(Exception):
    """Minimal stand-in for WorkflowBackendError used in tests."""

    def __init__(self, message: str, *, reason_code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status


def _run_create_tasks_handle(
    monkeypatch,
    *,
    tasks_content=_TASKS_MD,
    create_tasks_side_effect=None,
    create_tasks_return=None,
    workspace_id=_WORKSPACE_ID,
    feature_id=_FEATURE_ID,
    context_workspace_id=_WORKSPACE_ID,
    context_feature_id=_FEATURE_ID,
    read_document_raises=None,
    read_document_content=None,
):
    """Run handle() with all external calls mocked.

    Patches are applied directly on the loaded module's namespace.
    """
    monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
    monkeypatch.setenv("MANAGEMENT_REPO_BASE_BRANCH", _BASE_BRANCH)

    create_mock = MagicMock(return_value=create_tasks_return or {"tasks": []})
    if create_tasks_side_effect is not None:
        create_mock.side_effect = create_tasks_side_effect

    mod = _load_create_tasks_mod()

    # Patch module-level imports in create_tasks's namespace via handle()'s inline imports.
    # We need to patch the modules that handle() imports inline.

    # Patch plugins.context
    ctx_mock = MagicMock()
    ctx_mock.get_workspace_id.return_value = context_workspace_id
    ctx_mock.get_feature_id.return_value = context_feature_id
    sys.modules["plugins.context"] = ctx_mock

    # Patch plugins.document_repo (git path — unused by the default owner="go"
    # feature detail below, since tasks.md for go lives in storage-service).
    doc_repo_mock = MagicMock()
    if read_document_raises:
        doc_repo_mock.read_document.side_effect = read_document_raises
    else:
        content = (
            read_document_content
            if read_document_content is not None
            else tasks_content
        )
        doc_repo_mock.read_document.return_value = {"content": content, "sha": "sha123"}
    sys.modules["plugins.document_repo"] = doc_repo_mock

    # Patch plugins.storage_service_client — go-owned features (the default
    # here) read tasks.md from storage-service, not git.
    storage_mock = MagicMock()
    if read_document_raises:
        storage_mock.read_document_content.side_effect = read_document_raises
    else:
        content = (
            read_document_content
            if read_document_content is not None
            else tasks_content
        )
        storage_mock.read_document_content.return_value = {
            "content": content,
            "version_id": "v1",
        }
    sys.modules["plugins.storage_service_client"] = storage_mock

    # Patch plugins.tools.approve — provides _resolve_status_branch_and_path and
    # _run_async_create_tasks
    approve_mock = MagicMock()
    approve_mock._resolve_status_branch_and_path.return_value = (
        _FEATURE_BRANCH,
        f"docs/features/{_FEATURE_ID}/status.yaml",
    )
    approve_mock._run_async_create_tasks = create_mock
    sys.modules["plugins.tools.approve"] = approve_mock

    # Patch plugins.tools.artifacts — provides _resolve_management_repo
    artifacts_mock = MagicMock()
    artifacts_mock._resolve_management_repo.return_value = (_OWNER, _REPO)
    sys.modules["plugins.tools.artifacts"] = artifacts_mock

    # Patch src.services.workflow_backend_client — provides WorkflowBackendError and
    # the workspace/feature lookups load_feature_tasks_md() runs via run_async().
    wbc_mock = MagicMock()
    wbc_mock.WorkflowBackendError = _WorkflowBackendError
    wbc_mock.get_workspace_context.return_value = _make_workspace_context()
    wbc_mock.get_feature_detail.return_value = _make_feature_detail()
    wbc_mock.run_async.side_effect = lambda coro: coro
    sys.modules["src"] = MagicMock()
    sys.modules["src.services"] = MagicMock()
    sys.modules["src.services.workflow_backend_client"] = wbc_mock

    result = mod.handle(workspace_id=workspace_id, feature_id=feature_id)
    return result, create_mock


# ---------------------------------------------------------------------------
# _relay_create_tasks_reason_code
# ---------------------------------------------------------------------------


class TestRelayCreateTasksReasonCode:
    def test_feature_not_tasks_approved_points_at_approve_command(self):
        mod = _load_create_tasks_mod()
        msg = mod._relay_create_tasks_reason_code("feature_not_tasks_approved")
        assert "approve command" in msg
        assert "a→b→c" in msg

    def test_feature_not_tasks_approved_mentions_retry(self):
        mod = _load_create_tasks_mod()
        msg = mod._relay_create_tasks_reason_code("feature_not_tasks_approved")
        assert "retry" in msg.lower() or "retry" in msg

    def test_missing_config_mentions_env_vars(self):
        mod = _load_create_tasks_mod()
        msg = mod._relay_create_tasks_reason_code("missing_config")
        assert "WORKFLOW_BACKEND_URL" in msg or "WORKFLOW_BACKEND_SERVICE_TOKEN" in msg

    def test_empty_tasks_mentions_index_table(self):
        mod = _load_create_tasks_mod()
        msg = mod._relay_create_tasks_reason_code("empty_tasks")
        assert "Index" in msg or "tasks.md" in msg

    def test_unknown_code_returns_fallback(self):
        mod = _load_create_tasks_mod()
        msg = mod._relay_create_tasks_reason_code("totally_unknown_code")
        assert isinstance(msg, str)
        assert len(msg) > 0

    def test_feature_not_tasks_approved_is_string(self):
        mod = _load_create_tasks_mod()
        assert isinstance(
            mod._relay_create_tasks_reason_code("feature_not_tasks_approved"), str
        )


# ---------------------------------------------------------------------------
# handle() — missing context / config guards
# ---------------------------------------------------------------------------


class TestHandleMissingContext:
    def test_missing_workspace_and_feature_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        mod = _load_create_tasks_mod()

        ctx_mock = MagicMock()
        ctx_mock.get_workspace_id.return_value = ""
        ctx_mock.get_feature_id.return_value = ""
        sys.modules["plugins.context"] = ctx_mock

        result = mod.handle(workspace_id="", feature_id="")
        assert result["ok"] is False
        assert "workspace_id" in result["error"] or "context" in result["error"]

    def test_missing_workspace_only_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        mod = _load_create_tasks_mod()

        ctx_mock = MagicMock()
        ctx_mock.get_workspace_id.return_value = ""
        ctx_mock.get_feature_id.return_value = _FEATURE_ID
        sys.modules["plugins.context"] = ctx_mock

        result = mod.handle(workspace_id="", feature_id="")
        assert result["ok"] is False

    def test_missing_feature_only_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        mod = _load_create_tasks_mod()

        ctx_mock = MagicMock()
        ctx_mock.get_workspace_id.return_value = _WORKSPACE_ID
        ctx_mock.get_feature_id.return_value = ""
        sys.modules["plugins.context"] = ctx_mock

        result = mod.handle(workspace_id="", feature_id="")
        assert result["ok"] is False

    def test_missing_github_token_returns_error(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        mod = _load_create_tasks_mod()

        ctx_mock = MagicMock()
        ctx_mock.get_workspace_id.return_value = _WORKSPACE_ID
        ctx_mock.get_feature_id.return_value = _FEATURE_ID
        sys.modules["plugins.context"] = ctx_mock

        result = mod.handle(workspace_id=_WORKSPACE_ID, feature_id=_FEATURE_ID)
        assert result["ok"] is False
        assert "GITHUB_TOKEN" in result["error"]


class TestHandleManagementRepoFailure:
    def test_repo_resolution_failure_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        mod = _load_create_tasks_mod()

        ctx_mock = MagicMock()
        ctx_mock.get_workspace_id.return_value = _WORKSPACE_ID
        ctx_mock.get_feature_id.return_value = _FEATURE_ID
        sys.modules["plugins.context"] = ctx_mock

        # Only replace the leaf module — plugins.tools.approve (imported for real
        # below) needs the real src/src.services packages to resolve its own
        # src.services.approval_notifications import.
        wbc_mock = MagicMock()
        wbc_mock.WorkflowBackendError = _WorkflowBackendError
        wbc_mock.get_workspace_context.side_effect = RuntimeError("workflow-backend offline")
        sys.modules["src.services.workflow_backend_client"] = wbc_mock

        artifacts_mock = MagicMock()
        artifacts_mock._resolve_management_repo.side_effect = ValueError("no repo")
        sys.modules["plugins.tools.artifacts"] = artifacts_mock

        result = mod.handle(workspace_id=_WORKSPACE_ID, feature_id=_FEATURE_ID)
        assert result["ok"] is False
        assert (
            "management repo" in result["error"].lower()
            or "Could not" in result["error"]
        )


# ---------------------------------------------------------------------------
# handle() — tasks.md read
# ---------------------------------------------------------------------------


class TestHandleTasksMdRead:
    def test_read_failure_returns_error(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch, read_document_raises=RuntimeError("network error")
        )
        assert result["ok"] is False
        assert "tasks.md" in result["error"]

    def test_empty_tasks_md_returns_error(self, monkeypatch):
        result, _ = _run_create_tasks_handle(monkeypatch, read_document_content="")
        assert result["ok"] is False
        assert "tasks.md" in result["error"]


# ---------------------------------------------------------------------------
# handle() — success path
# ---------------------------------------------------------------------------


class TestHandleSuccessPath:
    def test_success_returns_ok_true(self, monkeypatch):
        result, _ = _run_create_tasks_handle(monkeypatch)
        assert result["ok"] is True

    def test_success_message_present(self, monkeypatch):
        result, _ = _run_create_tasks_handle(monkeypatch)
        assert "message" in result
        assert "created" in result["message"].lower()

    def test_success_calls_create_tasks(self, monkeypatch):
        _, create_mock = _run_create_tasks_handle(monkeypatch)
        assert create_mock.called

    def test_success_passes_workspace_id(self, monkeypatch):
        _, create_mock = _run_create_tasks_handle(monkeypatch)
        call_args = create_mock.call_args
        assert _WORKSPACE_ID in (call_args.args + tuple(call_args.kwargs.values()))

    def test_success_passes_feature_id(self, monkeypatch):
        _, create_mock = _run_create_tasks_handle(monkeypatch)
        call_args = create_mock.call_args
        assert _FEATURE_ID in (call_args.args + tuple(call_args.kwargs.values()))

    def test_success_passes_parsed_tasks(self, monkeypatch):
        _, create_mock = _run_create_tasks_handle(monkeypatch)
        call_args = create_mock.call_args
        all_args = call_args.args + tuple(call_args.kwargs.values())
        # The parsed task list (not the raw tasks.md) is passed to create.
        task_lists = [a for a in all_args if isinstance(a, list)]
        assert task_lists, "expected a parsed task list argument"
        names = [t["name"] for t in task_lists[0]]
        assert names == ["T1", "T2"]

    def test_result_included_in_response(self, monkeypatch):
        backend_result = {"tasks": [{"id": "T1"}]}
        result, _ = _run_create_tasks_handle(
            monkeypatch, create_tasks_return=backend_result
        )
        assert result.get("result") == backend_result


# ---------------------------------------------------------------------------
# handle() — tasks_already_exist (safe no-op)
# ---------------------------------------------------------------------------


class TestHandleTasksAlreadyExist:
    def test_tasks_already_exist_returns_ok_true(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "tasks exist", reason_code="tasks_already_exist"
            ),
        )
        assert result["ok"] is True

    def test_tasks_already_exist_sets_noop(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "tasks exist", reason_code="tasks_already_exist"
            ),
        )
        assert result.get("noop") is True

    def test_tasks_already_exist_message_says_nothing_to_do(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "tasks exist", reason_code="tasks_already_exist"
            ),
        )
        msg = result.get("message", "")
        assert "nothing to do" in msg.lower() or "already exist" in msg.lower()

    def test_tasks_already_exist_does_not_include_reason_code_as_error(
        self, monkeypatch
    ):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "tasks exist", reason_code="tasks_already_exist"
            ),
        )
        # No error field expected on a no-op success
        assert "error" not in result


# ---------------------------------------------------------------------------
# handle() — feature_not_tasks_approved (guard reject)
# ---------------------------------------------------------------------------


class TestHandleFeatureNotTasksApproved:
    def test_not_approved_returns_ok_false(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "not approved", reason_code="feature_not_tasks_approved"
            ),
        )
        assert result["ok"] is False

    def test_not_approved_surfaces_reason_code(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "not approved", reason_code="feature_not_tasks_approved"
            ),
        )
        assert result.get("reason_code") == "feature_not_tasks_approved"

    def test_not_approved_error_mentions_approve_command(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "not approved", reason_code="feature_not_tasks_approved"
            ),
        )
        assert "approve" in result["error"].lower()

    def test_not_approved_error_points_at_abc_steps(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "not approved", reason_code="feature_not_tasks_approved"
            ),
        )
        assert "a→b→c" in result["error"] or "approve" in result["error"].lower()

    def test_not_approved_no_creation_attempted_once_guard_fires(self, monkeypatch):
        """The create call raises; ensure it was called exactly once (no retry)."""
        _, create_mock = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "not approved", reason_code="feature_not_tasks_approved"
            ),
        )
        assert create_mock.call_count == 1


# ---------------------------------------------------------------------------
# handle() — missing_config guard relay
# ---------------------------------------------------------------------------


class TestHandleMissingConfig:
    def test_missing_config_returns_ok_false(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "missing config", reason_code="missing_config"
            ),
        )
        assert result["ok"] is False

    def test_missing_config_error_mentions_env_vars(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "missing config", reason_code="missing_config"
            ),
        )
        err = result["error"]
        assert "WORKFLOW_BACKEND_URL" in err or "WORKFLOW_BACKEND_SERVICE_TOKEN" in err


# ---------------------------------------------------------------------------
# handle() — other/unknown WorkflowBackendError
# ---------------------------------------------------------------------------


class TestHandleUnknownBackendError:
    def test_unknown_reason_returns_ok_false(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "something weird", reason_code="unknown_code"
            ),
        )
        assert result["ok"] is False

    def test_unknown_reason_error_is_non_empty_string(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=_WorkflowBackendError(
                "something weird", reason_code="unknown_code"
            ),
        )
        assert isinstance(result.get("error"), str)
        assert len(result["error"]) > 0

    def test_generic_exception_returns_ok_false(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=RuntimeError("connection reset"),
        )
        assert result["ok"] is False

    def test_generic_exception_error_mentions_failure(self, monkeypatch):
        result, _ = _run_create_tasks_handle(
            monkeypatch,
            create_tasks_side_effect=RuntimeError("connection reset"),
        )
        assert (
            "failed" in result["error"].lower() or "connection reset" in result["error"]
        )


# ---------------------------------------------------------------------------
# handle() — parameter override
# ---------------------------------------------------------------------------


class TestHandleParameterOverride:
    def test_explicit_workspace_id_overrides_context(self, monkeypatch):
        """When workspace_id is passed explicitly, it takes precedence over context."""
        _, create_mock = _run_create_tasks_handle(
            monkeypatch,
            workspace_id="explicit-ws",
            context_workspace_id="context-ws",
        )
        call_args = create_mock.call_args
        all_args = call_args.args + tuple(call_args.kwargs.values())
        assert any("explicit-ws" in str(a) for a in all_args)

    def test_explicit_feature_id_overrides_context(self, monkeypatch):
        """When feature_id is passed explicitly, it takes precedence over context."""
        _, create_mock = _run_create_tasks_handle(
            monkeypatch,
            feature_id="explicit-feat",
            context_feature_id="context-feat",
        )
        call_args = create_mock.call_args
        all_args = call_args.args + tuple(call_args.kwargs.values())
        assert any("explicit-feat" in str(a) for a in all_args)

    def test_context_fallback_used_when_no_explicit_id(self, monkeypatch):
        """When workspace_id is omitted, the context value is used."""
        _, create_mock = _run_create_tasks_handle(
            monkeypatch,
            workspace_id="",
            context_workspace_id=_WORKSPACE_ID,
        )
        assert create_mock.called


# ---------------------------------------------------------------------------
# _TOOLS registration
# ---------------------------------------------------------------------------


class TestToolsRegistration:
    def test_create_tasks_in_tools_list(self):
        from plugins import _TOOLS

        names = [t["name"] for t in _TOOLS]
        assert "create_tasks" in names

    def test_create_tasks_has_required_fields(self):
        from plugins import _TOOLS

        tool = next(t for t in _TOOLS if t["name"] == "create_tasks")
        assert "schema" in tool
        assert "handler" in tool
        assert "check_fn" in tool

    def test_register_includes_create_tasks(self):
        ctx = MagicMock()
        from plugins import register

        register(ctx)
        registered_names = [
            call.kwargs.get("name") or call.args[0]
            for call in ctx.register_tool.call_args_list
        ]
        assert "create_tasks" in registered_names
