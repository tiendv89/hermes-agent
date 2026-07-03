"""Tests for T4 — workflow-backend service client.

Covers:
  - parse_tasks_md_index: happy path (basic table), no depends, multi-depends,
    empty/missing table, backtick stripping
  - _build_headers: header keys and values from identity args
  - _extract_reason_code: various backend error shapes
  - create_feature_tasks:
      - missing WORKFLOW_BACKEND_URL raises WorkflowBackendError(missing_config)
      - missing WORKFLOW_BACKEND_SERVICE_TOKEN raises WorkflowBackendError(missing_config)
      - empty tasks.md raises WorkflowBackendError(empty_tasks)
      - 2xx returns parsed body
      - 4xx with reason_code raises WorkflowBackendError with reason_code surfaced
      - 4xx without reason_code raises WorkflowBackendError with empty reason_code
      - header construction uses identity from context (X-User-Id / X-Org-Id)
      - payload includes parsed tasks
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio  # noqa: F401 — registers the asyncio backend

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Pre-load plugins.context without triggering plugins/__init__.py.
#
# plugins/__init__.py imports many heavy tools (mcp, requests, etc.) that may
# not be installed in CI. The client only needs plugins.context — load it
# directly from its file so the module is cached in sys.modules before any
# test import touches the plugins package.
# ---------------------------------------------------------------------------

def _ensure_plugins_context() -> None:
    if "plugins.context" in sys.modules:
        return
    ctx_path = REPO_ROOT / "plugins" / "context.py"
    spec = importlib.util.spec_from_file_location("plugins.context", ctx_path)
    mod = importlib.util.module_from_spec(spec)
    # Register parent package stub so `from plugins.context import X` resolves.
    if "plugins" not in sys.modules:
        import types
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(REPO_ROOT / "plugins")]
        pkg.__package__ = "plugins"
        sys.modules["plugins"] = pkg
    sys.modules["plugins.context"] = mod
    spec.loader.exec_module(mod)


_ensure_plugins_context()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SAMPLE_TASKS_MD = """\
# Tasks — go-orchestrator-ui-integration

## Index

| ID | Wave | Title | Repo | Depends on |
|----|------|-------|------|------------|
| T1 | 1 | Thread `user_id` + `org_id` into tool context | hermes-agent | — |
| T2 | 1 | `write_tasks`: go branch stops at `tasks.md` | hermes-agent | — |
| T3 | 1 | `CreateTasks` server-side guard | workflow-backend | — |
| T4 | 2 | workflow-backend service client | hermes-agent | T1 |
| T5 | 3 | Tasks-stage approve orchestration | hermes-agent | T2, T3, T4 |

## T1 — Thread user_id + org_id
"""


def _import_client():
    import src.services.workflow_backend_client as mod

    return (
        mod.WorkflowBackendError,
        mod._build_headers,
        mod._extract_reason_code,
        mod.create_feature_tasks,
        mod.parse_tasks_md_index,
    )


# ---------------------------------------------------------------------------
# parse_tasks_md_index
# ---------------------------------------------------------------------------


class TestParseTasksMdIndex:
    def test_parses_all_tasks(self):
        _, _, _, _, parse = _import_client()
        tasks = parse(_SAMPLE_TASKS_MD)
        assert len(tasks) == 5

    def test_task_ids_correct(self):
        _, _, _, _, parse = _import_client()
        tasks = parse(_SAMPLE_TASKS_MD)
        assert [t["id"] for t in tasks] == ["T1", "T2", "T3", "T4", "T5"]

    def test_no_depends_parsed_as_empty_list(self):
        _, _, _, _, parse = _import_client()
        tasks = parse(_SAMPLE_TASKS_MD)
        assert tasks[0]["depends_on"] == []
        assert tasks[1]["depends_on"] == []
        assert tasks[2]["depends_on"] == []

    def test_single_depends_parsed(self):
        _, _, _, _, parse = _import_client()
        tasks = parse(_SAMPLE_TASKS_MD)
        # T4 depends on T1
        t4 = next(t for t in tasks if t["id"] == "T4")
        assert t4["depends_on"] == ["T1"]

    def test_multi_depends_parsed(self):
        _, _, _, _, parse = _import_client()
        tasks = parse(_SAMPLE_TASKS_MD)
        # T5 depends on T2, T3, T4
        t5 = next(t for t in tasks if t["id"] == "T5")
        assert t5["depends_on"] == ["T2", "T3", "T4"]

    def test_repo_field_populated(self):
        _, _, _, _, parse = _import_client()
        tasks = parse(_SAMPLE_TASKS_MD)
        assert tasks[0]["repo"] == "hermes-agent"
        assert tasks[2]["repo"] == "workflow-backend"

    def test_backticks_stripped_from_title(self):
        _, _, _, _, parse = _import_client()
        tasks = parse(_SAMPLE_TASKS_MD)
        # T1 title has backtick spans
        assert "`" not in tasks[0]["title"]
        assert "user_id" in tasks[0]["title"]

    def test_actor_type_defaults_to_agent(self):
        _, _, _, _, parse = _import_client()
        tasks = parse(_SAMPLE_TASKS_MD)
        for t in tasks:
            assert t["actor_type"] == "agent"

    def test_empty_tasks_md_returns_empty_list(self):
        _, _, _, _, parse = _import_client()
        assert parse("") == []

    def test_no_index_table_returns_empty_list(self):
        _, _, _, _, parse = _import_client()
        md = "# Tasks\n\nJust some text, no index table.\n"
        assert parse(md) == []

    def test_table_with_em_dash_depends_on(self):
        _, _, _, _, parse = _import_client()
        md = (
            "## Index\n\n"
            "| ID | Wave | Title | Repo | Depends on |\n"
            "|----|------|-------|------|-----------|\n"
            "| T1 | 1 | My task | my-repo | — |\n"
        )
        tasks = parse(md)
        assert tasks[0]["depends_on"] == []


# ---------------------------------------------------------------------------
# _build_headers
# ---------------------------------------------------------------------------


class TestBuildHeaders:
    def test_authorization_header(self):
        _, build_headers, _, _, _ = _import_client()
        h = build_headers("uid-1", "org-1", "svc-token")
        assert h["Authorization"] == "Bearer svc-token"

    def test_x_user_id_header(self):
        _, build_headers, _, _, _ = _import_client()
        h = build_headers("uid-1", "org-1", "svc-token")
        assert h["X-User-Id"] == "uid-1"

    def test_x_org_id_header(self):
        _, build_headers, _, _, _ = _import_client()
        h = build_headers("uid-1", "org-1", "svc-token")
        assert h["X-Org-Id"] == "org-1"

    def test_x_accessible_org_ids_equals_org_id(self):
        _, build_headers, _, _, _ = _import_client()
        h = build_headers("uid-1", "org-42", "svc-token")
        assert h["X-Accessible-Org-Ids"] == "org-42"

    def test_content_type_is_json(self):
        _, build_headers, _, _, _ = _import_client()
        h = build_headers("u", "o", "t")
        assert h["Content-Type"] == "application/json"

    def test_empty_identity_values_accepted(self):
        _, build_headers, _, _, _ = _import_client()
        h = build_headers("", "", "tok")
        assert h["X-User-Id"] == ""
        assert h["X-Org-Id"] == ""


# ---------------------------------------------------------------------------
# _extract_reason_code
# ---------------------------------------------------------------------------


class TestExtractReasonCode:
    def test_reason_code_field(self):
        _, _, extract, _, _ = _import_client()
        assert extract({"reason_code": "tasks_already_exist"}) == "tasks_already_exist"

    def test_reason_field(self):
        _, _, extract, _, _ = _import_client()
        assert extract({"reason": "feature_not_tasks_approved"}) == "feature_not_tasks_approved"

    def test_error_string_field(self):
        _, _, extract, _, _ = _import_client()
        assert extract({"error": "tasks_already_exist"}) == "tasks_already_exist"

    def test_nested_error_dict(self):
        _, _, extract, _, _ = _import_client()
        assert extract({"error": {"code": "tasks_already_exist"}}) == "tasks_already_exist"

    def test_non_dict_body_returns_empty(self):
        _, _, extract, _, _ = _import_client()
        assert extract("plain string error") == ""
        assert extract(None) == ""
        assert extract([]) == ""

    def test_empty_dict_returns_empty(self):
        _, _, extract, _, _ = _import_client()
        assert extract({}) == ""


# ---------------------------------------------------------------------------
# create_feature_tasks — async tests
# ---------------------------------------------------------------------------


class TestCreateFeatureTasks:
    @pytest.mark.asyncio
    async def test_missing_url_raises_missing_config(self, monkeypatch):
        WorkflowBackendError, _, _, create, _ = _import_client()
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        with pytest.raises(WorkflowBackendError) as exc_info:
            await create("ws-1", "feat-1", _SAMPLE_TASKS_MD)
        assert exc_info.value.reason_code == "missing_config"

    @pytest.mark.asyncio
    async def test_missing_token_raises_missing_config(self, monkeypatch):
        WorkflowBackendError, _, _, create, _ = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)

        with pytest.raises(WorkflowBackendError) as exc_info:
            await create("ws-1", "feat-1", _SAMPLE_TASKS_MD)
        assert exc_info.value.reason_code == "missing_config"

    @pytest.mark.asyncio
    async def test_empty_tasks_md_raises_empty_tasks(self, monkeypatch):
        WorkflowBackendError, _, _, create, _ = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        with pytest.raises(WorkflowBackendError) as exc_info:
            await create("ws-1", "feat-1", "# Tasks\nNo index table here.")
        assert exc_info.value.reason_code == "empty_tasks"

    @pytest.mark.asyncio
    async def test_success_returns_body(self, monkeypatch):
        WorkflowBackendError, _, _, create, _ = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        fake_resp = MagicMock()
        fake_resp.status = 201
        fake_resp.json = AsyncMock(return_value={"tasks": [{"id": "T1"}]})
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        fake_session = MagicMock()
        fake_session.post = MagicMock(return_value=fake_resp)
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=fake_session),
            patch("plugins.context.get_user_id", return_value="u-1"),
            patch("plugins.context.get_org_id", return_value="o-1"),
        ):
            result = await create("ws-1", "feat-1", _SAMPLE_TASKS_MD)

        assert result == {"tasks": [{"id": "T1"}]}

    @pytest.mark.asyncio
    async def test_4xx_with_reason_code_raises_correct_code(self, monkeypatch):
        WorkflowBackendError, _, _, create, _ = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        fake_resp = MagicMock()
        fake_resp.status = 422
        fake_resp.json = AsyncMock(return_value={"error": "feature_not_tasks_approved"})
        fake_resp.text = AsyncMock(return_value="")
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        fake_session = MagicMock()
        fake_session.post = MagicMock(return_value=fake_resp)
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=fake_session),
            patch("plugins.context.get_user_id", return_value="u-1"),
            patch("plugins.context.get_org_id", return_value="o-1"),
        ):
            with pytest.raises(WorkflowBackendError) as exc_info:
                await create("ws-1", "feat-1", _SAMPLE_TASKS_MD)

        assert exc_info.value.reason_code == "feature_not_tasks_approved"
        assert exc_info.value.status == 422

    @pytest.mark.asyncio
    async def test_4xx_tasks_already_exist_surfaces_code(self, monkeypatch):
        WorkflowBackendError, _, _, create, _ = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        fake_resp = MagicMock()
        fake_resp.status = 409
        fake_resp.json = AsyncMock(return_value={"reason_code": "tasks_already_exist"})
        fake_resp.text = AsyncMock(return_value="")
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        fake_session = MagicMock()
        fake_session.post = MagicMock(return_value=fake_resp)
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=fake_session),
            patch("plugins.context.get_user_id", return_value="u-1"),
            patch("plugins.context.get_org_id", return_value="o-1"),
        ):
            with pytest.raises(WorkflowBackendError) as exc_info:
                await create("ws-1", "feat-1", _SAMPLE_TASKS_MD)

        assert exc_info.value.reason_code == "tasks_already_exist"

    @pytest.mark.asyncio
    async def test_4xx_without_reason_code_raises_empty_code(self, monkeypatch):
        WorkflowBackendError, _, _, create, _ = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        fake_resp = MagicMock()
        fake_resp.status = 500
        fake_resp.json = AsyncMock(return_value={"message": "internal server error"})
        fake_resp.text = AsyncMock(return_value="")
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        fake_session = MagicMock()
        fake_session.post = MagicMock(return_value=fake_resp)
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=fake_session),
            patch("plugins.context.get_user_id", return_value="u-1"),
            patch("plugins.context.get_org_id", return_value="o-1"),
        ):
            with pytest.raises(WorkflowBackendError) as exc_info:
                await create("ws-1", "feat-1", _SAMPLE_TASKS_MD)

        assert exc_info.value.reason_code == ""
        assert exc_info.value.status == 500

    @pytest.mark.asyncio
    async def test_headers_contain_identity_from_context(self, monkeypatch):
        """X-User-Id and X-Org-Id are sourced from T1-threaded context getters."""
        _, _, _, create, _ = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        captured_headers: dict = {}

        fake_resp = MagicMock()
        fake_resp.status = 201
        fake_resp.json = AsyncMock(return_value={"tasks": []})
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        def fake_post(url, *, headers, json, timeout):
            captured_headers.update(headers)
            return fake_resp

        fake_session = MagicMock()
        fake_session.post = fake_post
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=fake_session),
            patch("plugins.context.get_user_id", return_value="user-123"),
            patch("plugins.context.get_org_id", return_value="org-456"),
        ):
            await create("ws-1", "feat-1", _SAMPLE_TASKS_MD)

        assert captured_headers["X-User-Id"] == "user-123"
        assert captured_headers["X-Org-Id"] == "org-456"
        assert captured_headers["X-Accessible-Org-Ids"] == "org-456"
        assert captured_headers["Authorization"] == "Bearer svc-tok"

    @pytest.mark.asyncio
    async def test_payload_contains_parsed_tasks(self, monkeypatch):
        """The POST body must include all tasks parsed from the tasks.md index."""
        _, _, _, create, _ = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        captured_payload: dict = {}

        fake_resp = MagicMock()
        fake_resp.status = 201
        fake_resp.json = AsyncMock(return_value={"tasks": []})
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        def fake_post(url, *, headers, json, timeout):
            captured_payload.update(json)
            return fake_resp

        fake_session = MagicMock()
        fake_session.post = fake_post
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=fake_session),
            patch("plugins.context.get_user_id", return_value="u"),
            patch("plugins.context.get_org_id", return_value="o"),
        ):
            await create("ws-1", "feat-1", _SAMPLE_TASKS_MD)

        tasks = captured_payload.get("tasks", [])
        assert len(tasks) == 5
        assert tasks[0]["id"] == "T1"
        t4 = next(t for t in tasks if t["id"] == "T4")
        assert t4["depends_on"] == ["T1"]

    @pytest.mark.asyncio
    async def test_url_contains_workspace_and_feature(self, monkeypatch):
        """The POST endpoint URL must embed workspace_id and feature_id."""
        _, _, _, create, _ = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        captured_url: list = []

        fake_resp = MagicMock()
        fake_resp.status = 201
        fake_resp.json = AsyncMock(return_value={"tasks": []})
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        def fake_post(url, *, headers, json, timeout):
            captured_url.append(url)
            return fake_resp

        fake_session = MagicMock()
        fake_session.post = fake_post
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=fake_session),
            patch("plugins.context.get_user_id", return_value="u"),
            patch("plugins.context.get_org_id", return_value="o"),
        ):
            await create("my-workspace", "my-feature", _SAMPLE_TASKS_MD)

        assert len(captured_url) == 1
        assert "my-workspace" in captured_url[0]
        assert "my-feature" in captured_url[0]
