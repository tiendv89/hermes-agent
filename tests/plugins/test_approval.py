"""Tests for T3: workflow_request_approval tool + stage-transition endpoint + GET /tools.

Covers:
  - workflow_request_approval: returns payload, writes nothing
  - workflow_request_approval: missing feature_id returns error
  - workflow_request_approval: invalid stage returns error
  - stage-transition: approve mutations match approve-feature skill
  - stage-transition: reject mutations
  - stage-transition: reopen sets revalidation flags
  - stage-transition: missing status.yaml → 404 (go feature guard)
  - stage-transition: stale SHA → 409
  - GET /tools: returns live registry honoring check_fn
  - GET /tools: gated tools (check_fn returns False) are excluded
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Module cleanup fixture (same as other test files in this dir)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
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
    yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_STATUS_YAML_CONTENT = """\
feature_id: my-feature
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
  technical_design:
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

_STATUS_YAML_SHA = "abc123"
_GITHUB_TOKEN = "ghp_test"
_WORKSPACE_ID = "ws-test"
_OWNER = "testorg"
_REPO = "testws"
_FEATURE_ID = "my-feature"
_ACTOR = "user-42"


def _make_read_result(content=_STATUS_YAML_CONTENT, sha=_STATUS_YAML_SHA):
    return {"content": content, "sha": sha}


def _make_write_result():
    return {"commit_sha": "newsha123", "pr": {"url": "https://github.com/x/y/pull/1", "number": 1, "state": "open"}}


# ---------------------------------------------------------------------------
# workflow_request_approval — read-only tool
# ---------------------------------------------------------------------------


class TestWorkflowRequestApproval:
    def _import_handle(self):
        """Import approval.handle without triggering the shadow-package issue."""
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "plugins.tools.approval",
            REPO_ROOT / "plugins" / "tools" / "approval.py",
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "plugins.tools"
        sys.modules["plugins.tools.approval"] = mod
        spec.loader.exec_module(mod)
        return mod.handle

    def test_returns_approval_request_payload(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("WORKSPACE_ID", _WORKSPACE_ID)
        handle = self._import_handle()

        with (
            patch("plugins.db.get_workspace_context", return_value={"management_repo": "mgmt", "repos": [{"id": "mgmt", "github": f"https://github.com/{_OWNER}/{_REPO}"}]}),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
        ):
            result = handle(stage="product_spec")

        assert result["ok"] is True
        assert result["approval_request"]["feature_id"] == _FEATURE_ID
        assert result["approval_request"]["stage"] == "product_spec"
        assert result["approval_request"]["review_status"] == "draft"

    def test_writes_nothing(self, monkeypatch):
        """The tool must never call write_document."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("WORKSPACE_ID", _WORKSPACE_ID)
        handle = self._import_handle()

        with (
            patch("plugins.db.get_workspace_context", return_value={"management_repo": "mgmt", "repos": [{"id": "mgmt", "github": f"https://github.com/{_OWNER}/{_REPO}"}]}),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document") as mock_write,
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
        ):
            handle(stage="product_spec")

        mock_write.assert_not_called()

    def test_missing_feature_id_returns_error(self, monkeypatch):
        handle = self._import_handle()

        with patch("plugins.context.get_feature_id", return_value=""):
            result = handle(stage="product_spec", feature_id="")

        assert result["ok"] is False
        assert "feature_id" in result["error"]

    def test_invalid_stage_returns_error(self, monkeypatch):
        handle = self._import_handle()

        with patch("plugins.context.get_feature_id", return_value=_FEATURE_ID):
            result = handle(stage="nonexistent_stage", feature_id=_FEATURE_ID)

        assert result["ok"] is False
        assert "stage" in result["error"].lower()

    def test_explicit_feature_id_overrides_context(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("WORKSPACE_ID", _WORKSPACE_ID)
        handle = self._import_handle()

        with (
            patch("plugins.db.get_workspace_context", return_value={"management_repo": "mgmt", "repos": [{"id": "mgmt", "github": f"https://github.com/{_OWNER}/{_REPO}"}]}),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()) as mock_read,
            patch("plugins.context.get_feature_id", return_value="context-feature"),
        ):
            result = handle(stage="product_spec", feature_id="explicit-feature")

        assert result["ok"] is True
        assert result["approval_request"]["feature_id"] == "explicit-feature"
        # Verify the read was called on the explicit feature branch.
        call_args = mock_read.call_args
        assert "feature/explicit-feature" in call_args[0]

    def test_read_review_status_uses_approved_from_yaml(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("WORKSPACE_ID", _WORKSPACE_ID)
        handle = self._import_handle()

        approved_yaml = _STATUS_YAML_CONTENT.replace(
            "review_status: draft", "review_status: approved", 1
        )
        with (
            patch("plugins.db.get_workspace_context", return_value={"management_repo": "mgmt", "repos": [{"id": "mgmt", "github": f"https://github.com/{_OWNER}/{_REPO}"}]}),
            patch("plugins.document_repo.read_document", return_value={"content": approved_yaml, "sha": "s1"}),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
        ):
            result = handle(stage="product_spec", feature_id=_FEATURE_ID)

        assert result["ok"] is True
        assert result["approval_request"]["review_status"] == "approved"

    def test_missing_github_token_returns_unknown(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("WORKSPACE_ID", raising=False)
        handle = self._import_handle()

        with patch("plugins.context.get_feature_id", return_value=_FEATURE_ID):
            result = handle(stage="product_spec", feature_id=_FEATURE_ID)

        assert result["ok"] is True
        # No token → _read_review_status returns "unknown" (not an error)
        assert result["approval_request"]["review_status"] == "unknown"

    def test_status_yaml_absent_returns_draft(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("WORKSPACE_ID", _WORKSPACE_ID)
        handle = self._import_handle()

        with (
            patch("plugins.db.get_workspace_context", return_value={"management_repo": "mgmt", "repos": [{"id": "mgmt", "github": f"https://github.com/{_OWNER}/{_REPO}"}]}),
            patch("plugins.document_repo.read_document", return_value={"content": "", "sha": None}),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
        ):
            result = handle(stage="product_spec", feature_id=_FEATURE_ID)

        assert result["ok"] is True
        assert result["approval_request"]["review_status"] == "draft"


# ---------------------------------------------------------------------------
# GET /tools — live registry list honouring check_fn
# ---------------------------------------------------------------------------


class TestGetToolsEndpoint:
    """Test the GET /tools route via the FastAPI test client."""

    def _build_client(self, monkeypatch):
        """Build a TestClient for the router module, bypassing DB setup."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.api.identity import Identity, require_identity

        app = FastAPI()
        from src.api import router as router_mod
        app.include_router(router_mod.router, prefix="/api/v1")
        # Override the dependency so calls don't touch the DB.
        app.dependency_overrides[require_identity] = lambda: Identity(user_id="test-user")
        return TestClient(app)

    def test_returns_tools_json(self, monkeypatch):
        monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
        monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
        monkeypatch.delenv("RAG_MCP_URL", raising=False)
        # Patch _TOOLS to a known fixture.
        fake_tools = (
            {"name": "tool_alpha", "schema": {"description": "Alpha tool."}, "check_fn": None},
            {"name": "tool_beta", "schema": {"description": "Beta tool."}, "check_fn": lambda: True},
        )
        with patch("src.api.router._TOOLS", new=fake_tools, create=True):
            with patch("plugins._TOOLS", fake_tools):
                client = self._build_client(monkeypatch)
                resp = client.get("/api/v1/tools")

        assert resp.status_code == 200
        data = resp.json()
        assert "tools" in data
        names = [t["name"] for t in data["tools"]]
        assert "tool_alpha" in names
        assert "tool_beta" in names

    def test_gated_tool_excluded(self, monkeypatch):
        """A tool whose check_fn returns False must not appear in the list."""
        fake_tools = (
            {"name": "always_on", "schema": {"description": "Always available."}, "check_fn": None},
            {"name": "gated_off", "schema": {"description": "Gated."}, "check_fn": lambda: False},
        )
        with patch("plugins._TOOLS", fake_tools):
            client = self._build_client(monkeypatch)
            resp = client.get("/api/v1/tools")

        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["tools"]]
        assert "always_on" in names
        assert "gated_off" not in names

    def test_tool_description_from_schema(self, monkeypatch):
        fake_tools = (
            {"name": "described", "schema": {"description": "My tool description."}, "check_fn": None},
        )
        with patch("plugins._TOOLS", fake_tools):
            client = self._build_client(monkeypatch)
            resp = client.get("/api/v1/tools")

        tools = resp.json()["tools"]
        assert tools[0]["description"] == "My tool description."

    def test_missing_schema_description_falls_back_to_empty_string(self, monkeypatch):
        fake_tools = (
            {"name": "nodesc", "schema": {}, "check_fn": None},
        )
        with patch("plugins._TOOLS", fake_tools):
            client = self._build_client(monkeypatch)
            resp = client.get("/api/v1/tools")

        tools = resp.json()["tools"]
        assert tools[0]["description"] == ""

    def test_workflow_request_approval_in_live_registry(self, monkeypatch):
        """When WORKFLOW_DATABASE_URL is set, workflow_request_approval must appear."""
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
        client = self._build_client(monkeypatch)
        resp = client.get("/api/v1/tools")
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["tools"]]
        assert "workflow_request_approval" in names

    def test_workflow_request_approval_excluded_when_db_unset(self, monkeypatch):
        """When WORKFLOW_DATABASE_URL is unset, workflow_request_approval is excluded."""
        monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
        client = self._build_client(monkeypatch)
        resp = client.get("/api/v1/tools")
        assert resp.status_code == 200
        names = [t["name"] for t in resp.json()["tools"]]
        assert "workflow_request_approval" not in names


# ---------------------------------------------------------------------------
# POST /features/{feature_id}/stage-transition
# ---------------------------------------------------------------------------


class TestStageTransitionEndpoint:
    def _build_client(self, monkeypatch, actor=_ACTOR):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from src.api.identity import Identity, require_identity

        app = FastAPI()
        from src.api import router as router_mod
        app.include_router(router_mod.router, prefix="/api/v1")
        # Override the dependency so calls don't touch the DB.
        app.dependency_overrides[require_identity] = lambda: Identity(user_id=actor)
        return TestClient(app)

    def _patch_context(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.setenv("WORKSPACE_ID", _WORKSPACE_ID)

    def _workspace_ctx(self):
        return {
            "management_repo": "mgmt",
            "repos": [{"id": "mgmt", "github": f"https://github.com/{_OWNER}/{_REPO}"}],
        }

    # ---- approve ----

    def test_approve_sets_review_status(self, monkeypatch):
        self._patch_context(monkeypatch)
        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document", return_value=_make_write_result()),
        ):
            client = self._build_client(monkeypatch)
            resp = client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "approve"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["review_status"] == "approved"
        assert data["action"] == "approve"

    def test_approve_advances_feature_status(self, monkeypatch):
        """Approving product_spec must advance feature_status to in_tdd."""
        self._patch_context(monkeypatch)
        written_content = {}

        def _capture_write(owner, repo, feature_id, base_branch, path, content, sha, msg, token):
            written_content["content"] = content
            return _make_write_result()

        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document", side_effect=_capture_write),
        ):
            client = self._build_client(monkeypatch)
            client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "approve"},
            )

        import yaml
        parsed = yaml.safe_load(written_content["content"])
        assert parsed["feature_status"] == "in_tdd"
        assert parsed["current_stage"] == "technical_design"

    def test_approve_appends_review_history(self, monkeypatch):
        self._patch_context(monkeypatch)
        written_content = {}

        def _capture(owner, repo, fid, base, path, content, sha, msg, token):
            written_content["content"] = content
            return _make_write_result()

        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document", side_effect=_capture),
        ):
            client = self._build_client(monkeypatch)
            client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "approve", "comment": "LGTM"},
            )

        import yaml
        parsed = yaml.safe_load(written_content["content"])
        history = parsed["stages"]["product_spec"]["review_history"]
        assert len(history) == 1
        assert history[0]["review_status"] == "approved"
        assert history[0]["reviewed_by"] == _ACTOR
        assert history[0]["comment"] == "LGTM"

    def test_approve_appends_top_level_history(self, monkeypatch):
        self._patch_context(monkeypatch)
        written_content = {}

        def _capture(owner, repo, fid, base, path, content, sha, msg, token):
            written_content["content"] = content
            return _make_write_result()

        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document", side_effect=_capture),
        ):
            client = self._build_client(monkeypatch)
            client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "approve"},
            )

        import yaml
        parsed = yaml.safe_load(written_content["content"])
        assert len(parsed["history"]) == 1
        assert parsed["history"][0]["action"] == "stage_approved"
        assert parsed["history"][0]["stage"] == "product_spec"

    def test_approve_does_not_merge_pr(self, monkeypatch):
        """Approve writes status.yaml but must never merge the PR."""
        self._patch_context(monkeypatch)
        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document", return_value=_make_write_result()),
            patch("requests.delete") as mock_delete,
            patch("requests.put") as mock_put,
        ):
            client = self._build_client(monkeypatch)
            client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "approve"},
            )

        # No DELETE or PUT to a /merges endpoint.
        for call in mock_delete.call_args_list + mock_put.call_args_list:
            url = call[0][0] if call[0] else call[1].get("url", "")
            assert "merge" not in str(url).lower()

    # ---- reject ----

    def test_reject_sets_rejected_status(self, monkeypatch):
        self._patch_context(monkeypatch)
        written_content = {}

        def _capture(owner, repo, fid, base, path, content, sha, msg, token):
            written_content["content"] = content
            return _make_write_result()

        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document", side_effect=_capture),
        ):
            client = self._build_client(monkeypatch)
            resp = client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "reject", "comment": "Needs more detail"},
            )

        assert resp.status_code == 200
        assert resp.json()["review_status"] == "rejected"

        import yaml
        parsed = yaml.safe_load(written_content["content"])
        stage = parsed["stages"]["product_spec"]
        assert stage["review_status"] == "rejected"
        assert stage["review_comment"] == "Needs more detail"
        # Reject does NOT change feature_status (stays in_design).
        assert parsed["feature_status"] == "in_design"

    def test_reject_appends_history_entry(self, monkeypatch):
        self._patch_context(monkeypatch)
        written_content = {}

        def _capture(owner, repo, fid, base, path, content, sha, msg, token):
            written_content["content"] = content
            return _make_write_result()

        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document", side_effect=_capture),
        ):
            client = self._build_client(monkeypatch)
            client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "reject", "comment": "Bad"},
            )

        import yaml
        parsed = yaml.safe_load(written_content["content"])
        assert parsed["history"][0]["action"] == "stage_rejected"

    # ---- reopen ----

    def test_reopen_sets_draft_and_revalidation_flags(self, monkeypatch):
        """Reopen product_spec must set technical_design_required and tasks_required."""
        self._patch_context(monkeypatch)

        approved_status = _STATUS_YAML_CONTENT.replace(
            "feature_status: in_design", "feature_status: in_tdd"
        ).replace(
            "review_status: draft\n    reviewed_by: null\n    reviewed_at: null\n    review_comment: null\n    review_history: []",
            "review_status: approved\n    reviewed_by: someone\n    reviewed_at: '2026-01-01'\n    review_comment: null\n    review_history: []",
            1,
        )
        written_content = {}

        def _capture(owner, repo, fid, base, path, content, sha, msg, token):
            written_content["content"] = content
            return _make_write_result()

        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value={"content": approved_status, "sha": _STATUS_YAML_SHA}),
            patch("plugins.document_repo.write_document", side_effect=_capture),
        ):
            client = self._build_client(monkeypatch)
            resp = client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "reopen"},
            )

        assert resp.status_code == 200
        import yaml
        parsed = yaml.safe_load(written_content["content"])
        assert parsed["stages"]["product_spec"]["review_status"] == "draft"
        assert parsed["revalidation"]["technical_design_required"] is True
        assert parsed["revalidation"]["tasks_required"] is True
        assert parsed["feature_status"] == "in_design"
        assert parsed["current_stage"] == "product_spec"

    def test_reopen_appends_history_entry(self, monkeypatch):
        self._patch_context(monkeypatch)
        written_content = {}

        def _capture(owner, repo, fid, base, path, content, sha, msg, token):
            written_content["content"] = content
            return _make_write_result()

        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document", side_effect=_capture),
        ):
            client = self._build_client(monkeypatch)
            client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "reopen"},
            )

        import yaml
        parsed = yaml.safe_load(written_content["content"])
        assert parsed["history"][0]["action"] == "stage_reopened"

    # ---- error cases ----

    def test_missing_status_yaml_returns_404(self, monkeypatch):
        """A feature with no status.yaml (go feature guard) must return 404."""
        self._patch_context(monkeypatch)
        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value={"content": "", "sha": None}),
        ):
            client = self._build_client(monkeypatch)
            resp = client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "approve"},
            )

        assert resp.status_code == 404

    def test_stale_sha_returns_409(self, monkeypatch):
        self._patch_context(monkeypatch)
        from plugins.document_repo import StaleBaseError

        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document", side_effect=StaleBaseError("status.yaml", "sha mismatch")),
        ):
            client = self._build_client(monkeypatch)
            resp = client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "approve"},
            )

        assert resp.status_code == 409

    def test_invalid_stage_returns_400(self, monkeypatch):
        self._patch_context(monkeypatch)
        client = self._build_client(monkeypatch)
        resp = client.post(
            f"/api/v1/features/{_FEATURE_ID}/stage-transition",
            json={"stage": "not_a_stage", "action": "approve"},
        )
        assert resp.status_code == 400

    def test_invalid_action_returns_400(self, monkeypatch):
        self._patch_context(monkeypatch)
        client = self._build_client(monkeypatch)
        resp = client.post(
            f"/api/v1/features/{_FEATURE_ID}/stage-transition",
            json={"stage": "product_spec", "action": "delete"},
        )
        assert resp.status_code == 400

    def test_missing_github_token_returns_500(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.setenv("WORKSPACE_ID", _WORKSPACE_ID)
        client = self._build_client(monkeypatch)
        resp = client.post(
            f"/api/v1/features/{_FEATURE_ID}/stage-transition",
            json={"stage": "product_spec", "action": "approve"},
        )
        assert resp.status_code == 500

    def test_missing_workspace_id_returns_500(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        monkeypatch.delenv("WORKSPACE_ID", raising=False)
        client = self._build_client(monkeypatch)
        resp = client.post(
            f"/api/v1/features/{_FEATURE_ID}/stage-transition",
            json={"stage": "product_spec", "action": "approve"},
        )
        assert resp.status_code == 500

    def test_actor_recorded_as_x_user_id(self, monkeypatch):
        """The actor written to status.yaml must come from X-User-Id (Identity.user_id)."""
        self._patch_context(monkeypatch)
        written_content = {}

        def _capture(owner, repo, fid, base, path, content, sha, msg, token):
            written_content["content"] = content
            return _make_write_result()

        with (
            patch("plugins.db.get_workspace_context", return_value=self._workspace_ctx()),
            patch("plugins.document_repo.read_document", return_value=_make_read_result()),
            patch("plugins.document_repo.write_document", side_effect=_capture),
        ):
            client = self._build_client(monkeypatch, actor="user-from-header")
            client.post(
                f"/api/v1/features/{_FEATURE_ID}/stage-transition",
                json={"stage": "product_spec", "action": "approve"},
            )

        import yaml
        parsed = yaml.safe_load(written_content["content"])
        assert parsed["stages"]["product_spec"]["reviewed_by"] == "user-from-header"
