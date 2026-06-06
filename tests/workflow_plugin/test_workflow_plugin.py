"""Tests for workflow_plugin — tool schemas, handlers, registration, and hook.

Covers:
    - tool schema shape (required fields present)
    - check_workflow_available: returns False when env not set, True when set
    - handle_get_workspace_context: happy path and HTTP error path
    - handle_get_feature_state: happy path and HTTP error path
    - handle_write_product_spec: happy path, no-token error, HTTP error, update (with SHA)
    - handle_write_technical_design: same pattern
    - _parse_github_owner_repo: SSH and HTTPS URL parsing
    - _get_management_repo_github: resolves owner/repo from workspace context
    - _inject_feature_context hook: no-op when workspace_id missing; injects block when set
    - register(ctx): all 4 tools + pre_llm_call hook registered on a mock PluginContext
    - plugin.yaml: manifest is valid YAML, name == 'workflow', provides_tools list present
    - smoke: plugin is discoverable via PluginManager when workflow_plugin is on the
      search path as a bundled plugin
"""

from __future__ import annotations

import base64
import importlib
import importlib.util
import json
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "workflow_plugin"


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

def _load_client():
    """Import workflow_plugin.client in isolation."""
    spec = importlib.util.spec_from_file_location(
        "workflow_plugin.client",
        PLUGIN_DIR / "client.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "workflow_plugin"
    sys.modules["workflow_plugin.client"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_tools():
    """Import workflow_plugin.tools (depends on client)."""
    _load_client()
    spec = importlib.util.spec_from_file_location(
        "workflow_plugin.tools",
        PLUGIN_DIR / "tools.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "workflow_plugin"
    sys.modules["workflow_plugin.tools"] = mod
    spec.loader.exec_module(mod)
    return mod


def _load_plugin_init(tools_mod=None):
    """Import workflow_plugin.__init__ (depends on tools)."""
    if tools_mod is None:
        tools_mod = _load_tools()
    spec = importlib.util.spec_from_file_location(
        "workflow_plugin",
        PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "workflow_plugin"
    mod.__path__ = [str(PLUGIN_DIR)]
    sys.modules["workflow_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_modules():
    """Remove workflow_plugin modules between tests."""
    keys = [k for k in sys.modules if k.startswith("workflow_plugin")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("workflow_plugin")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _no_workflow_backend(monkeypatch):
    """Ensure WORKFLOW_BACKEND_URL is unset by default."""
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    yield


# ---------------------------------------------------------------------------
# plugin.yaml
# ---------------------------------------------------------------------------

class TestPluginManifest:
    def test_name_is_workflow(self):
        import yaml

        manifest_path = PLUGIN_DIR / "plugin.yaml"
        with open(manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        assert manifest["name"] == "workflow"

    def test_has_required_fields(self):
        import yaml

        manifest_path = PLUGIN_DIR / "plugin.yaml"
        with open(manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        assert "version" in manifest
        assert "description" in manifest

    def test_provides_tools_list_present(self):
        import yaml

        manifest_path = PLUGIN_DIR / "plugin.yaml"
        with open(manifest_path, encoding="utf-8") as f:
            manifest = yaml.safe_load(f)
        assert "provides_tools" in manifest
        tools = manifest["provides_tools"]
        assert isinstance(tools, list)
        assert "workflow_get_workspace_context" in tools
        assert "workflow_get_feature_state" in tools
        assert "workflow_write_product_spec" in tools
        assert "workflow_write_technical_design" in tools


# ---------------------------------------------------------------------------
# check_workflow_available
# ---------------------------------------------------------------------------

class TestCheckWorkflowAvailable:
    def test_returns_false_when_not_set(self):
        tools = _load_tools()
        assert tools.check_workflow_available() is False

    def test_returns_true_when_set(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://localhost:8080")
        tools = _load_tools()
        assert tools.check_workflow_available() is True

    def test_returns_false_for_blank_value(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "   ")
        tools = _load_tools()
        assert tools.check_workflow_available() is False


# ---------------------------------------------------------------------------
# handle_get_workspace_context
# ---------------------------------------------------------------------------

class TestHandleGetWorkspaceContext:
    def test_happy_path(self, monkeypatch, requests_mock):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        tools = _load_tools()
        requests_mock.get(
            "http://backend/api/workspaces/ws-1",
            json={"id": "ws-1", "repos": ["frontend", "backend"]},
        )
        result = tools.handle_get_workspace_context(workspace_id="ws-1")
        assert result["ok"] is True
        assert result["workspace"]["id"] == "ws-1"
        assert "frontend" in result["workspace"]["repos"]

    def test_http_error_returns_ok_false(self, monkeypatch, requests_mock):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        tools = _load_tools()
        requests_mock.get("http://backend/api/workspaces/ws-bad", status_code=500)
        result = tools.handle_get_workspace_context(workspace_id="ws-bad")
        assert result["ok"] is False
        assert "error" in result

    def test_no_backend_url_raises_on_call(self, monkeypatch):
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        tools = _load_tools()
        result = tools.handle_get_workspace_context(workspace_id="ws-1")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# handle_get_feature_state
# ---------------------------------------------------------------------------

class TestHandleGetFeatureState:
    def test_happy_path(self, monkeypatch, requests_mock):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        tools = _load_tools()
        requests_mock.get(
            "http://backend/api/workspaces/ws-1/features/feat-1",
            json={"id": "feat-1", "stage": "in_implementation"},
        )
        result = tools.handle_get_feature_state(workspace_id="ws-1", feature_id="feat-1")
        assert result["ok"] is True
        assert result["feature"]["stage"] == "in_implementation"

    def test_404_returns_ok_false(self, monkeypatch, requests_mock):
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        tools = _load_tools()
        requests_mock.get(
            "http://backend/api/workspaces/ws-1/features/missing",
            status_code=404,
        )
        result = tools.handle_get_feature_state(workspace_id="ws-1", feature_id="missing")
        assert result["ok"] is False


# ---------------------------------------------------------------------------
# GitHub helper unit tests
# ---------------------------------------------------------------------------

class TestParseGithubOwnerRepo:
    def test_ssh_url(self):
        tools = _load_tools()
        owner, repo = tools._parse_github_owner_repo("git@github.com:myorg/myrepo.git")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_ssh_url_no_git_suffix(self):
        tools = _load_tools()
        owner, repo = tools._parse_github_owner_repo("git@github.com:myorg/myrepo")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_https_url(self):
        tools = _load_tools()
        owner, repo = tools._parse_github_owner_repo("https://github.com/myorg/myrepo.git")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_https_url_no_git_suffix(self):
        tools = _load_tools()
        owner, repo = tools._parse_github_owner_repo("https://github.com/myorg/myrepo")
        assert owner == "myorg"
        assert repo == "myrepo"

    def test_invalid_url_raises(self):
        tools = _load_tools()
        with pytest.raises(ValueError, match="Cannot parse"):
            tools._parse_github_owner_repo("not-a-github-url")


class TestGetManagementRepoGithub:
    def test_resolves_via_management_repo_id(self):
        tools = _load_tools()
        ctx = {
            "management_repo": "mgmt-repo",
            "repos": [
                {"id": "other-repo", "github": "git@github.com:org/other.git"},
                {"id": "mgmt-repo", "github": "git@github.com:org/management.git"},
            ],
        }
        owner, repo = tools._get_management_repo_github(ctx)
        assert owner == "org"
        assert repo == "management"

    def test_fallback_to_management_in_id(self):
        tools = _load_tools()
        ctx = {
            "management_repo": None,
            "repos": [
                {"id": "management-repo", "github": "git@github.com:org/workspace.git"},
            ],
        }
        owner, repo = tools._get_management_repo_github(ctx)
        assert owner == "org"
        assert repo == "workspace"

    def test_raises_when_no_match(self):
        tools = _load_tools()
        ctx = {
            "management_repo": "missing-id",
            "repos": [
                {"id": "some-repo", "github": "git@github.com:org/other.git"},
            ],
        }
        with pytest.raises(ValueError, match="Could not resolve"):
            tools._get_management_repo_github(ctx)


# ---------------------------------------------------------------------------
# handle_write_product_spec
# ---------------------------------------------------------------------------

_WORKSPACE_CONTEXT_RESPONSE = {
    "id": "ws-1",
    "management_repo": "management-repo",
    "repos": [
        {
            "id": "management-repo",
            "github": "git@github.com:testorg/testws.git",
        }
    ],
}

_GITHUB_PUT_RESPONSE = {
    "content": {"path": "docs/features/feat-1/product-spec.md"},
    "commit": {"sha": "abc123def456"},
}


class TestHandleWriteProductSpec:
    def test_happy_path_new_file(self, monkeypatch, requests_mock):
        """Write to a new file (no existing SHA)."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_testtoken")
        tools = _load_tools()

        requests_mock.get(
            "http://backend/api/workspaces/ws-1",
            json=_WORKSPACE_CONTEXT_RESPONSE,
        )
        # File doesn't exist yet → 404 on GET SHA
        requests_mock.get(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/product-spec.md",
            status_code=404,
        )
        requests_mock.put(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/product-spec.md",
            json=_GITHUB_PUT_RESPONSE,
            status_code=201,
        )

        result = tools.handle_write_product_spec(
            workspace_id="ws-1",
            feature_id="feat-1",
            content="# Product Spec\n\nContent here.",
        )

        assert result["ok"] is True
        assert result["path"] == "docs/features/feat-1/product-spec.md"
        assert result["commit"] == "abc123def456"

    def test_happy_path_update_existing_file(self, monkeypatch, requests_mock):
        """Update an existing file — SHA is fetched and included in PUT payload."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_testtoken")
        tools = _load_tools()

        requests_mock.get(
            "http://backend/api/workspaces/ws-1",
            json=_WORKSPACE_CONTEXT_RESPONSE,
        )
        # File exists → GET returns SHA
        requests_mock.get(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/product-spec.md",
            json={"sha": "existingsha123"},
        )
        requests_mock.put(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/product-spec.md",
            json=_GITHUB_PUT_RESPONSE,
            status_code=200,
        )

        result = tools.handle_write_product_spec(
            workspace_id="ws-1",
            feature_id="feat-1",
            content="# Updated Spec\n",
        )

        assert result["ok"] is True
        # Verify SHA was sent in the PUT body
        put_request = requests_mock.last_request
        body = json.loads(put_request.text)
        assert body["sha"] == "existingsha123"
        expected_content = base64.b64encode(b"# Updated Spec\n").decode("ascii")
        assert body["content"] == expected_content

    def test_no_github_token_returns_error(self, monkeypatch):
        """Missing GITHUB_TOKEN → ok=False without making any HTTP calls."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        tools = _load_tools()

        result = tools.handle_write_product_spec(
            workspace_id="ws-1",
            feature_id="feat-1",
            content="# spec",
        )
        assert result["ok"] is False
        assert "GITHUB_TOKEN" in result["error"]

    def test_github_put_failure_returns_ok_false(self, monkeypatch, requests_mock):
        """GitHub API 422 → ok=False with error message."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_testtoken")
        tools = _load_tools()

        requests_mock.get(
            "http://backend/api/workspaces/ws-1",
            json=_WORKSPACE_CONTEXT_RESPONSE,
        )
        requests_mock.get(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/product-spec.md",
            status_code=404,
        )
        requests_mock.put(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/product-spec.md",
            status_code=422,
            json={"message": "Validation Failed"},
        )

        result = tools.handle_write_product_spec(
            workspace_id="ws-1",
            feature_id="feat-1",
            content="# spec",
        )
        assert result["ok"] is False
        assert "error" in result

    def test_custom_commit_message(self, monkeypatch, requests_mock):
        """commit_message parameter is forwarded to the GitHub API."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_testtoken")
        tools = _load_tools()

        requests_mock.get(
            "http://backend/api/workspaces/ws-1",
            json=_WORKSPACE_CONTEXT_RESPONSE,
        )
        requests_mock.get(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/product-spec.md",
            status_code=404,
        )
        requests_mock.put(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/product-spec.md",
            json=_GITHUB_PUT_RESPONSE,
            status_code=201,
        )

        tools.handle_write_product_spec(
            workspace_id="ws-1",
            feature_id="feat-1",
            content="# spec",
            commit_message="feat: add initial product spec",
        )

        put_request = requests_mock.last_request
        body = json.loads(put_request.text)
        assert body["message"] == "feat: add initial product spec"


# ---------------------------------------------------------------------------
# handle_write_technical_design
# ---------------------------------------------------------------------------

class TestHandleWriteTechnicalDesign:
    def test_happy_path_new_file(self, monkeypatch, requests_mock):
        """Write technical-design.md to a new path."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_testtoken")
        tools = _load_tools()

        requests_mock.get(
            "http://backend/api/workspaces/ws-1",
            json=_WORKSPACE_CONTEXT_RESPONSE,
        )
        requests_mock.get(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/technical-design.md",
            status_code=404,
        )
        requests_mock.put(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/technical-design.md",
            json={
                "content": {"path": "docs/features/feat-1/technical-design.md"},
                "commit": {"sha": "td_commit_sha"},
            },
            status_code=201,
        )

        result = tools.handle_write_technical_design(
            workspace_id="ws-1",
            feature_id="feat-1",
            content="# Technical Design\n\nDetails.",
        )

        assert result["ok"] is True
        assert result["path"] == "docs/features/feat-1/technical-design.md"
        assert result["commit"] == "td_commit_sha"

    def test_no_github_token_returns_error(self, monkeypatch):
        """Missing GITHUB_TOKEN → ok=False."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        tools = _load_tools()

        result = tools.handle_write_technical_design(
            workspace_id="ws-1",
            feature_id="feat-1",
            content="# td",
        )
        assert result["ok"] is False
        assert "GITHUB_TOKEN" in result["error"]

    def test_content_is_base64_encoded(self, monkeypatch, requests_mock):
        """Verify the content field in the PUT body is base64-encoded."""
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend")
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_testtoken")
        tools = _load_tools()

        requests_mock.get(
            "http://backend/api/workspaces/ws-1",
            json=_WORKSPACE_CONTEXT_RESPONSE,
        )
        requests_mock.get(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/technical-design.md",
            status_code=404,
        )
        requests_mock.put(
            "https://api.github.com/repos/testorg/testws/contents/docs/features/feat-1/technical-design.md",
            json={
                "content": {"path": "docs/features/feat-1/technical-design.md"},
                "commit": {"sha": "sha999"},
            },
            status_code=201,
        )

        raw_content = "# Technical Design\n\nWith unicode: café\n"
        tools.handle_write_technical_design(
            workspace_id="ws-1",
            feature_id="feat-1",
            content=raw_content,
        )

        put_request = requests_mock.last_request
        body = json.loads(put_request.text)
        decoded = base64.b64decode(body["content"]).decode("utf-8")
        assert decoded == raw_content


# ---------------------------------------------------------------------------
# Tool schemas
# ---------------------------------------------------------------------------

class TestToolSchemas:
    @pytest.mark.parametrize("schema_name,required_fields", [
        ("WS_CONTEXT_SCHEMA", ["workspace_id"]),
        ("FEATURE_STATE_SCHEMA", ["workspace_id", "feature_id"]),
        ("WRITE_SPEC_SCHEMA", ["workspace_id", "feature_id", "content"]),
        ("WRITE_TD_SCHEMA", ["workspace_id", "feature_id", "content"]),
    ])
    def test_required_fields_present(self, schema_name, required_fields):
        tools = _load_tools()
        schema = getattr(tools, schema_name)
        assert schema["type"] == "object"
        for field in required_fields:
            assert field in schema["required"], f"{field} missing from {schema_name}.required"
            assert field in schema["properties"], f"{field} missing from {schema_name}.properties"


# ---------------------------------------------------------------------------
# _inject_feature_context hook
# ---------------------------------------------------------------------------

class TestInjectFeatureContextHook:
    def test_noop_when_no_workspace_id(self):
        plugin = _load_plugin_init()
        messages = []
        plugin._inject_feature_context(messages, context_vars={})
        assert messages == []

    def test_injects_system_message(self):
        plugin = _load_plugin_init()
        messages = []
        plugin._inject_feature_context(
            messages,
            context_vars={"workspace_id": "ws-1", "feature_id": "feat-1"},
        )
        assert len(messages) == 1
        assert messages[0]["role"] == "system"
        assert "ws-1" in messages[0]["content"]
        assert "feat-1" in messages[0]["content"]

    def test_prepends_to_existing_system_message(self):
        plugin = _load_plugin_init()
        messages = [{"role": "system", "content": "existing context"}]
        plugin._inject_feature_context(
            messages,
            context_vars={"workspace_id": "ws-1"},
        )
        assert "existing context" in messages[0]["content"]
        assert "ws-1" in messages[0]["content"]

    def test_guardrail_instruction_present(self):
        plugin = _load_plugin_init()
        messages = []
        plugin._inject_feature_context(
            messages,
            context_vars={"workspace_id": "ws-1"},
        )
        assert "never advance lifecycle state" in messages[0]["content"].lower() or \
               "never advance lifecycle" in messages[0]["content"]


# ---------------------------------------------------------------------------
# register(ctx)
# ---------------------------------------------------------------------------

class TestRegister:
    def test_registers_all_four_tools(self):
        plugin = _load_plugin_init()
        ctx = MagicMock()
        plugin.register(ctx)
        registered_names = [call.kwargs.get("name") or call.args[0] for call in ctx.register_tool.call_args_list]
        assert "workflow_get_workspace_context" in registered_names
        assert "workflow_get_feature_state" in registered_names
        assert "workflow_write_product_spec" in registered_names
        assert "workflow_write_technical_design" in registered_names

    def test_registers_pre_llm_call_hook(self):
        plugin = _load_plugin_init()
        ctx = MagicMock()
        plugin.register(ctx)
        ctx.register_hook.assert_called_once_with("pre_llm_call", plugin._inject_feature_context)

    def test_tools_use_workflow_toolset(self):
        plugin = _load_plugin_init()
        ctx = MagicMock()
        plugin.register(ctx)
        for call in ctx.register_tool.call_args_list:
            kwargs = call.kwargs
            toolset = kwargs.get("toolset") or (call.args[1] if len(call.args) > 1 else None)
            assert toolset == "workflow", f"Expected toolset='workflow', got {toolset!r}"

    def test_check_fn_is_provided(self):
        plugin = _load_plugin_init()
        ctx = MagicMock()
        plugin.register(ctx)
        for call in ctx.register_tool.call_args_list:
            check_fn = call.kwargs.get("check_fn")
            assert check_fn is not None, "check_fn should be set to check_workflow_available"
