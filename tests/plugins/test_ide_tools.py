"""Tests for coding-profile deferred-execution tools (T3).

Covers:
  - Schemas: every tool schema is a valid JSON Schema dict with required fields
  - Handlers: return ``{"__deferred__": True, "tool": "...", "params": {...}}``
  - Validation: handlers reject missing required params with ``ok: False``
  - deferred() helper: produces correct marker structure
  - Profile setup: ``register_tools()`` registers all expected coding tools
  - Deferred-result passthrough: ``_json_result_handler`` skips guardrail
    sanitization when the handler returns a ``__deferred__`` marker
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import ClassVar
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    """Remove plugins/profiles modules between tests to avoid cross-test
    pollution from module-level caches."""
    keys = [
        k
        for k in sys.modules
        if k.startswith(("plugins", "profiles", "src"))
    ]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [
        k
        for k in sys.modules
        if k.startswith(("plugins", "profiles", "src"))
    ]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    """Ensure MCP/workflow-backend URL env vars are unset by default."""
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
    yield


# ---------------------------------------------------------------------------
# Import helpers — avoid the tests/ shadow package
# ---------------------------------------------------------------------------


def _load_plugins_register():
    """Load plugins/__init__.py directly from REPO_ROOT."""
    init_path = REPO_ROOT / "plugins" / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "plugins",
        init_path,
        submodule_search_locations=[str(REPO_ROOT / "plugins")],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "plugins"
    mod.__path__ = [str(REPO_ROOT / "plugins")]
    sys.modules["plugins"] = mod
    spec.loader.exec_module(mod)
    return mod


def _get_coding_tools():
    """Return the coding profile's tool tuple (fresh import each call)."""
    import importlib

    _load_plugins_register()
    import src.tool_setup as coding_setup

    importlib.reload(coding_setup)
    return coding_setup._CODING_TOOLS


def _register_coding_tools(ctx):
    """Register all coding tools on a mock context."""
    import plugins

    plugins.register(ctx, tools=_get_coding_tools())


# ---------------------------------------------------------------------------
# deferred() helper
# ---------------------------------------------------------------------------


class TestDeferredHelper:
    def test_returns_correct_structure(self):
        from plugins.tools.deferred import deferred

        result = deferred("edit_file", {"path": "a.py", "edits": []})
        assert result == {
            "__deferred__": True,
            "tool": "edit_file",
            "params": {"path": "a.py", "edits": []},
        }

    def test_tool_name_is_string(self):
        from plugins.tools.deferred import deferred

        result = deferred("run_command", {"command": "ls"})
        assert isinstance(result["tool"], str)
        assert result["tool"] == "run_command"

    def test_params_can_be_empty(self):
        from plugins.tools.deferred import deferred

        result = deferred("git_status", {})
        assert result["params"] == {}


# ---------------------------------------------------------------------------
# Schema validation — every tool schema is a valid JSON Schema dict
# ---------------------------------------------------------------------------


class TestCodingToolSchemas:
    """Verify every coding tool schema has the required JSON Schema keys."""

    def _collect_schemas(self):
        tools = _get_coding_tools()
        return [(t["name"], t["schema"]) for t in tools]

    def test_all_tool_schemas_have_parameters(self):
        for name, schema in self._collect_schemas():
            assert "parameters" in schema, f"{name} schema missing 'parameters'"
            params = schema["parameters"]
            assert isinstance(params, dict), f"{name} parameters must be dict"
            assert params.get("type") == "object", f"{name} parameters type must be 'object'"
            assert "properties" in params, f"{name} parameters missing 'properties'"

    def test_all_tool_schemas_have_description(self):
        for name, schema in self._collect_schemas():
            assert "description" in schema, f"{name} schema missing 'description'"
            assert isinstance(schema["description"], str)
            assert len(schema["description"]) > 0

    def test_all_tool_schemas_have_additionalProperties_false(self):
        for name, schema in self._collect_schemas():
            params = schema["parameters"]
            assert params.get("additionalProperties") is False, (
                f"{name} parameters must have additionalProperties: false"
            )

    def test_required_params_are_subset_of_properties(self):
        for name, schema in self._collect_schemas():
            params = schema["parameters"]
            required = set(params.get("required", []))
            properties = set(params["properties"].keys())
            extra = required - properties
            assert not extra, (
                f"{name} required fields {extra} not in properties {properties}"
            )


# ---------------------------------------------------------------------------
# Handler tests — local_file_ops
# ---------------------------------------------------------------------------


class TestLocalFileOpsHandlers:
    """Test handlers in plugins/tools/local_file_ops.py."""

    def test_handle_read_file_returns_deferred(self):
        from plugins.tools.local_file_ops import handle_read_file

        result = handle_read_file(path="src/main.py")
        assert result["__deferred__"] is True
        assert result["tool"] == "read_file"
        assert result["params"] == {"path": "src/main.py"}

    def test_handle_read_file_with_line_range(self):
        from plugins.tools.local_file_ops import handle_read_file

        result = handle_read_file(path="src/main.py", start_line=10, end_line=20)
        assert result["params"] == {"path": "src/main.py", "start_line": 10, "end_line": 20}

    def test_handle_read_file_rejects_empty_path(self):
        from plugins.tools.local_file_ops import handle_read_file

        result = handle_read_file(path="")
        assert result["ok"] is False
        assert "path" in result["error"]

    def test_handle_edit_file_returns_deferred(self):
        from plugins.tools.local_file_ops import handle_edit_file

        edits = [{"old_string": "foo", "new_string": "bar"}]
        result = handle_edit_file(path="a.py", edits=edits)
        assert result["__deferred__"] is True
        assert result["tool"] == "edit_file"
        assert result["params"] == {"path": "a.py", "edits": edits}

    def test_handle_edit_file_rejects_empty_path(self):
        from plugins.tools.local_file_ops import handle_edit_file

        result = handle_edit_file(path="", edits=[{"old_string": "x", "new_string": "y"}])
        assert result["ok"] is False
        assert "path" in result["error"]

    def test_handle_edit_file_rejects_empty_edits(self):
        from plugins.tools.local_file_ops import handle_edit_file

        result = handle_edit_file(path="a.py", edits=[])
        assert result["ok"] is False
        assert "edits" in result["error"]

    def test_handle_write_file_returns_deferred(self):
        from plugins.tools.local_file_ops import handle_write_file

        result = handle_write_file(path="new.py", content="print('hi')")
        assert result["__deferred__"] is True
        assert result["tool"] == "write_file"
        assert result["params"] == {"path": "new.py", "content": "print('hi')"}

    def test_handle_write_file_allows_empty_content(self):
        from plugins.tools.local_file_ops import handle_write_file

        result = handle_write_file(path="empty.txt", content="")
        assert result["__deferred__"] is True
        assert result["params"]["content"] == ""

    def test_handle_write_file_rejects_empty_path(self):
        from plugins.tools.local_file_ops import handle_write_file

        result = handle_write_file(path="", content="stuff")
        assert result["ok"] is False

    def test_handle_create_directory_returns_deferred(self):
        from plugins.tools.local_file_ops import handle_create_directory

        result = handle_create_directory(path="src/middleware/")
        assert result["__deferred__"] is True
        assert result["tool"] == "create_directory"
        assert result["params"] == {"path": "src/middleware/"}

    def test_handle_create_directory_rejects_empty_path(self):
        from plugins.tools.local_file_ops import handle_create_directory

        result = handle_create_directory(path="")
        assert result["ok"] is False

    def test_handle_browse_directory_defaults_to_root(self):
        from plugins.tools.local_file_ops import handle_browse_directory

        result = handle_browse_directory()
        assert result["__deferred__"] is True
        assert result["tool"] == "browse_directory"
        assert result["params"] == {}

    def test_handle_browse_directory_with_path(self):
        from plugins.tools.local_file_ops import handle_browse_directory

        result = handle_browse_directory(path="src/")
        assert result["params"] == {"path": "src/"}

    def test_handle_search_code_returns_deferred(self):
        from plugins.tools.local_file_ops import handle_search_code

        result = handle_search_code(pattern="def test_")
        assert result["__deferred__"] is True
        assert result["tool"] == "search_code"
        assert result["params"] == {"pattern": "def test_"}

    def test_handle_search_code_with_filters(self):
        from plugins.tools.local_file_ops import handle_search_code

        result = handle_search_code(
            pattern="TODO", path="src/", file_glob="*.py"
        )
        assert result["params"] == {
            "pattern": "TODO",
            "path": "src/",
            "file_glob": "*.py",
        }

    def test_handle_search_code_rejects_empty_pattern(self):
        from plugins.tools.local_file_ops import handle_search_code

        result = handle_search_code(pattern="")
        assert result["ok"] is False
        assert "pattern" in result["error"]

    def test_handle_search_files_returns_deferred(self):
        from plugins.tools.local_file_ops import handle_search_files

        result = handle_search_files(pattern="*.py")
        assert result["__deferred__"] is True
        assert result["tool"] == "search_files"
        assert result["params"] == {"pattern": "*.py"}

    def test_handle_search_files_with_path(self):
        from plugins.tools.local_file_ops import handle_search_files

        result = handle_search_files(pattern="**/test_*.ts", path="src/")
        assert result["params"] == {"pattern": "**/test_*.ts", "path": "src/"}

    def test_handle_search_files_rejects_empty_pattern(self):
        from plugins.tools.local_file_ops import handle_search_files

        result = handle_search_files(pattern="")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Handler tests — terminal
# ---------------------------------------------------------------------------


class TestTerminalHandler:
    def test_handle_run_command_returns_deferred(self):
        from plugins.tools.terminal import handle_run_command

        result = handle_run_command(command="pytest")
        assert result["__deferred__"] is True
        assert result["tool"] == "run_command"
        assert result["params"] == {"command": "pytest"}

    def test_handle_run_command_with_workdir_and_timeout(self):
        from plugins.tools.terminal import handle_run_command

        result = handle_run_command(
            command="npm test", workdir="packages/core", timeout=120
        )
        assert result["params"] == {
            "command": "npm test",
            "workdir": "packages/core",
            "timeout": 120,
        }

    def test_handle_run_command_rejects_empty_command(self):
        from plugins.tools.terminal import handle_run_command

        result = handle_run_command(command="")
        assert result["ok"] is False
        assert "command" in result["error"]


# ---------------------------------------------------------------------------
# Handler tests — git_ops
# ---------------------------------------------------------------------------


class TestGitOpsHandlers:
    def test_handle_git_status_returns_deferred(self):
        from plugins.tools.git_ops import handle_git_status

        result = handle_git_status()
        assert result["__deferred__"] is True
        assert result["tool"] == "git_status"
        assert result["params"] == {}

    def test_handle_git_diff_defaults(self):
        from plugins.tools.git_ops import handle_git_diff

        result = handle_git_diff()
        assert result["__deferred__"] is True
        assert result["tool"] == "git_diff"
        assert result["params"] == {}

    def test_handle_git_diff_staged_only(self):
        from plugins.tools.git_ops import handle_git_diff

        result = handle_git_diff(staged=True)
        assert result["params"] == {"staged": True}

    def test_handle_git_diff_unstaged_only(self):
        from plugins.tools.git_ops import handle_git_diff

        result = handle_git_diff(staged=False)
        assert result["params"] == {"staged": False}

    def test_handle_git_diff_with_path(self):
        from plugins.tools.git_ops import handle_git_diff

        result = handle_git_diff(path="src/main.py")
        assert result["params"] == {"path": "src/main.py"}

    def test_handle_git_commit_returns_deferred(self):
        from plugins.tools.git_ops import handle_git_commit

        result = handle_git_commit(message="feat: add login")
        assert result["__deferred__"] is True
        assert result["tool"] == "git_commit"
        assert result["params"] == {"message": "feat: add login"}

    def test_handle_git_commit_rejects_empty_message(self):
        from plugins.tools.git_ops import handle_git_commit

        result = handle_git_commit(message="")
        assert result["ok"] is False
        assert "message" in result["error"]

    def test_handle_git_push_defaults(self):
        from plugins.tools.git_ops import handle_git_push

        result = handle_git_push()
        assert result["__deferred__"] is True
        assert result["tool"] == "git_push"
        assert result["params"] == {}

    def test_handle_git_push_with_all_options(self):
        from plugins.tools.git_ops import handle_git_push

        result = handle_git_push(
            remote="upstream", branch="main", set_upstream=True
        )
        assert result["params"] == {
            "remote": "upstream",
            "branch": "main",
            "set_upstream": True,
        }

    def test_handle_git_checkout_returns_deferred(self):
        from plugins.tools.git_ops import handle_git_checkout

        result = handle_git_checkout(branch="feature/x")
        assert result["__deferred__"] is True
        assert result["tool"] == "git_checkout"
        assert result["params"] == {"branch": "feature/x"}

    def test_handle_git_checkout_create_branch(self):
        from plugins.tools.git_ops import handle_git_checkout

        result = handle_git_checkout(branch="feature/y", create=True)
        assert result["params"] == {"branch": "feature/y", "create": True}

    def test_handle_git_checkout_rejects_empty_branch(self):
        from plugins.tools.git_ops import handle_git_checkout

        result = handle_git_checkout(branch="")
        assert result["ok"] is False
        assert "branch" in result["error"]

    def test_handle_git_log_defaults(self):
        from plugins.tools.git_ops import handle_git_log

        result = handle_git_log()
        assert result["__deferred__"] is True
        assert result["tool"] == "git_log"
        assert result["params"] == {}

    def test_handle_git_log_with_count_and_branch(self):
        from plugins.tools.git_ops import handle_git_log

        result = handle_git_log(count=5, branch="develop")
        assert result["params"] == {"count": 5, "branch": "develop"}


# ---------------------------------------------------------------------------
# Profile setup — register_tools
# ---------------------------------------------------------------------------


class TestCodingProfileSetup:
    """Verify the coding profile setup registers all expected tools."""

    EXPECTED_TOOLS: ClassVar[set[str]] = {
        # Shared (server-executed)
        "query_rag",
        "query_gitnexus",
        "get_workspace_context",
        "get_feature_state",
        "get_tasks",
        "load_skill",
        # Coding (client-executed, deferred) — coding_-prefixed to avoid
        # colliding with the workflow profile's read_file/write_file/edit_file
        # in the shared tool registry.
        "coding_read_file",
        "coding_edit_file",
        "coding_write_file",
        "create_directory",
        "browse_directory",
        "search_code",
        "search_files",
        "run_command",
        "git_status",
        "git_diff",
        "git_commit",
        "git_push",
        "git_checkout",
        "git_log",
    }

    def test_registers_all_tools(self):
        ctx = MagicMock()
        _register_coding_tools(ctx)
        assert ctx.register_tool.call_count == len(self.EXPECTED_TOOLS)

    def test_all_tool_names_registered(self):
        ctx = MagicMock()
        _register_coding_tools(ctx)
        names = {
            call.kwargs.get("name") or call.args[0]
            for call in ctx.register_tool.call_args_list
        }
        assert names == self.EXPECTED_TOOLS

    def test_rag_and_gitnexus_are_async(self):
        ctx = MagicMock()
        _register_coding_tools(ctx)
        for expected_async in ("query_rag", "query_gitnexus"):
            call = next(
                c
                for c in ctx.register_tool.call_args_list
                if (c.kwargs.get("name") or c.args[0]) == expected_async
            )
            assert call.kwargs.get("is_async") is True, (
                f"{expected_async} should be async"
            )

    def test_deferred_tools_are_not_async(self):
        ctx = MagicMock()
        _register_coding_tools(ctx)
        deferred_names = {
            "coding_read_file",
            "coding_edit_file",
            "coding_write_file",
            "create_directory",
            "browse_directory",
            "search_code",
            "search_files",
            "run_command",
            "git_status",
            "git_diff",
            "git_commit",
            "git_push",
            "git_checkout",
            "git_log",
        }
        for call in ctx.register_tool.call_args_list:
            name = call.kwargs.get("name") or call.args[0]
            if name in deferred_names:
                assert not call.kwargs.get("is_async"), (
                    f"{name} should not be async"
                )

    def test_deferred_handler_returns_json_with_marker(self):
        """The registered handler for a deferred tool must JSON-encode the
        ``__deferred__`` marker dict (not pass a bare dict through)."""
        import json as _json

        ctx = MagicMock()
        _register_coding_tools(ctx)

        edit_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "coding_edit_file"
        )
        wrapped = edit_call.kwargs["handler"]

        out = wrapped(path="a.py", edits=[{"old_string": "x", "new_string": "y"}])
        assert isinstance(out, str), "deferred handler must return a JSON string"
        decoded = _json.loads(out)
        assert decoded["__deferred__"] is True
        assert decoded["tool"] == "edit_file"
        assert decoded["params"]["path"] == "a.py"

    def test_deferred_handler_validation_error_is_json_string(self):
        """When validation fails, the handler returns ``ok: False`` dict
        that must also be JSON-stringified by the wrapper."""
        import json as _json

        ctx = MagicMock()
        _register_coding_tools(ctx)

        edit_call = next(
            c
            for c in ctx.register_tool.call_args_list
            if (c.kwargs.get("name") or c.args[0]) == "coding_edit_file"
        )
        wrapped = edit_call.kwargs["handler"]

        # Missing required 'edits' — validation error
        out = wrapped(path="a.py", edits=[])
        assert isinstance(out, str)
        decoded = _json.loads(out)
        assert decoded["ok"] is False
        assert "__deferred__" not in decoded


# ---------------------------------------------------------------------------
# Deferred-result passthrough in _json_result_handler
# ---------------------------------------------------------------------------


class TestDeferredPassthrough:
    """Verify that the ``_json_result_handler`` wrapper skips guardrail
    sanitization when the handler returns a ``__deferred__`` marker."""

    def test_sync_handler_deferred_skips_sanitize(self):
        """When a handler returns ``__deferred__``, the wrapper must NOT
        call ``guardrails.sanitize_result``."""
        from plugins import _guardrails, _json_result_handler

        handler_called = False

        def fake_handler(**kwargs):
            nonlocal handler_called
            handler_called = True
            return {"__deferred__": True, "tool": "git_status", "params": {}}

        wrapped = _json_result_handler(fake_handler, is_async=False, tool_name="git_status")

        with patch.object(
            _guardrails, "sanitize_result", wraps=_guardrails.sanitize_result
        ) as mock_sanitize:
            result = wrapped()
            # sanitize_result should NOT have been called
            mock_sanitize.assert_not_called()

        assert handler_called
        import json

        decoded = json.loads(result)
        assert decoded["__deferred__"] is True

    def test_sync_handler_no_deferred_still_sanitizes(self):
        """A normal (non-deferred) handler result must still go through
        guardrail sanitization (existing behavior preserved)."""
        from plugins import _guardrails, _json_result_handler

        def fake_handler(**kwargs):
            return {"ok": True, "data": "hello"}

        wrapped = _json_result_handler(fake_handler, is_async=False, tool_name="read_file")

        with patch.object(
            _guardrails, "sanitize_result", wraps=_guardrails.sanitize_result
        ) as mock_sanitize:
            result = wrapped()
            mock_sanitize.assert_called_once()

        import json

        decoded = json.loads(result)
        assert decoded["ok"] is True


# ---------------------------------------------------------------------------
# Integration: coding tools don't cross-contaminate workflow tools
# ---------------------------------------------------------------------------


class TestProfileIsolation:
    """Verify the coding and workflow profiles register disjoint tool sets."""

    def test_no_workflow_mutation_tools_in_coding(self):
        """The coding profile must never register workflow mutation tools."""
        workflow_mutation_tools = {
            "approve_feature",
            "create_tasks",
            "write_product_spec",
            "write_technical_design",
            "write_tasks",
            "request_approval",
            "move_feature_status",
            "parse_tasks",
            "suggest_next_actions",
            "vcs_pr_context",
            "vcs_pr_review",
            "edit_document",
            "list_documents",
            "read_workspace_file",
            "workflow_init_feature",
            "workflow_lookup_feature",
            "vcs_create_pr",
            "vcs_ensure_branch",
            "vcs_commit_files",
        }
        ctx = MagicMock()
        _register_coding_tools(ctx)
        registered_names = {
            call.kwargs.get("name") or call.args[0]
            for call in ctx.register_tool.call_args_list
        }
        overlap = registered_names & workflow_mutation_tools
        assert not overlap, (
            f"Coding profile registered workflow mutation tools: {overlap}"
        )

    def test_no_coding_tools_in_workflow(self):
        """The workflow profile must never register coding tools."""
        # Import workflow tools fresh
        import importlib

        _load_plugins_register()
        import src.tool_setup as wf_setup

        importlib.reload(wf_setup)
        ctx = MagicMock()
        import plugins

        plugins.register(ctx, tools=wf_setup._WORKFLOW_TOOLS)

        registered_names = {
            call.kwargs.get("name") or call.args[0]
            for call in ctx.register_tool.call_args_list
        }

        coding_tool_names = {
            "run_command",
            "git_status",
            "git_diff",
            "git_commit",
            "git_push",
            "git_checkout",
            "git_log",
            "create_directory",
            "browse_directory",
            "search_code",
            "search_files",
        }
        overlap = registered_names & coding_tool_names
        assert not overlap, (
            f"Workflow profile registered coding tools: {overlap}"
        )
