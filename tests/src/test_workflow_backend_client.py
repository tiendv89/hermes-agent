"""Tests for the workflow-backend service client (HTTP only).

Task parsing lives in ``plugins.tools.parse_tasks`` (see
``tests/plugins/test_parse_tasks.py``). This client receives an already-parsed
task list and only builds the request + calls workflow-backend.

Covers:
  - _build_headers: header keys and values from identity args
  - _extract_reason_code: various backend error shapes
  - create_feature_tasks:
      - missing WORKFLOW_BACKEND_URL raises WorkflowBackendError(missing_config)
      - missing WORKFLOW_BACKEND_SERVICE_TOKEN raises WorkflowBackendError(missing_config)
      - empty task list raises WorkflowBackendError(empty_tasks)
      - 2xx returns parsed body
      - 4xx with reason_code raises WorkflowBackendError with reason_code surfaced
      - 4xx without reason_code raises WorkflowBackendError with empty reason_code
      - header construction uses identity from context (X-User-Id / X-Org-Id)
      - payload includes the supplied tasks verbatim
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
    # Other test files (e.g. test_stream_chat.py, test_cancel.py) stub
    # sys.modules["plugins.context"] with a bare types.ModuleType() that only
    # carries a few attributes they need. Since sys.modules is process-global,
    # such a stub can already be cached here by the time this file is
    # collected — checking for the module's presence alone isn't enough, we
    # need the real module with get_user_id/get_org_id.
    existing = sys.modules.get("plugins.context")
    if existing is not None and hasattr(existing, "get_user_id"):
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


@pytest.fixture(autouse=True)
def _plugins_context_is_real():
    # Other test files replace sys.modules["plugins.context"] with a stub
    # while they run (execution happens after collection, so the module-level
    # call above can't protect against a later swap). Re-assert before every
    # test in this file so patch("plugins.context.get_user_id", ...) always
    # resolves against the real module.
    _ensure_plugins_context()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# An already-parsed task list, keyed by the workflow-backend CreateTaskItem
# contract (name/title/repo/depends_on/actor_type) — what parse_tasks_index emits.
_SAMPLE_TASKS = [
    {"name": "T1", "title": "Thread user_id + org_id", "repo": "hermes-agent", "depends_on": [], "actor_type": "agent"},
    {"name": "T2", "title": "write_tasks stops at tasks.md", "repo": "hermes-agent", "depends_on": [], "actor_type": "agent"},
    {"name": "T3", "title": "CreateTasks server-side guard", "repo": "workflow-backend", "depends_on": [], "actor_type": "agent"},
    {"name": "T4", "title": "workflow-backend service client", "repo": "hermes-agent", "depends_on": ["T1"], "actor_type": "agent"},
    {"name": "T5", "title": "approve orchestration", "repo": "hermes-agent", "depends_on": ["T2", "T3", "T4"], "actor_type": "human"},
]


def _import_client():
    import src.services.workflow_backend_client as mod

    return (
        mod.WorkflowBackendError,
        mod._build_headers,
        mod._extract_reason_code,
        mod.create_feature_tasks,
    )


# ---------------------------------------------------------------------------
# _build_headers
# ---------------------------------------------------------------------------


class TestBuildHeaders:
    """_build_headers is async: X-Accessible-Org-Ids requires a user-service
    round trip (get_accessible_org_ids). monkeypatch clears USER_SERVICE_URL
    so these tests hit the "unavailable -> fall back to [org_id]" path
    without a real network call; TestBuildHeadersAccessibleOrgIds below
    covers the real multi-org lookup."""

    @pytest.mark.asyncio
    async def test_authorization_header(self, monkeypatch):
        monkeypatch.delenv("USER_SERVICE_URL", raising=False)
        _, build_headers, _, _ = _import_client()
        h = await build_headers("uid-1", "org-1", "svc-token")
        assert h["Authorization"] == "Bearer svc-token"

    @pytest.mark.asyncio
    async def test_x_user_id_header(self, monkeypatch):
        monkeypatch.delenv("USER_SERVICE_URL", raising=False)
        _, build_headers, _, _ = _import_client()
        h = await build_headers("uid-1", "org-1", "svc-token")
        assert h["X-User-Id"] == "uid-1"

    @pytest.mark.asyncio
    async def test_x_org_id_header(self, monkeypatch):
        monkeypatch.delenv("USER_SERVICE_URL", raising=False)
        _, build_headers, _, _ = _import_client()
        h = await build_headers("uid-1", "org-1", "svc-token")
        assert h["X-Org-Id"] == "org-1"

    @pytest.mark.asyncio
    async def test_x_accessible_org_ids_falls_back_to_org_id_when_unavailable(
        self, monkeypatch
    ):
        """When user-service's accessible-orgs lookup is unavailable, fall back
        to the single org_id — the old (imperfect) behavior — rather than an
        empty header."""
        monkeypatch.delenv("USER_SERVICE_URL", raising=False)
        _, build_headers, _, _ = _import_client()
        h = await build_headers("uid-1", "org-42", "svc-token")
        assert h["X-Accessible-Org-Ids"] == "org-42"

    @pytest.mark.asyncio
    async def test_content_type_is_json(self, monkeypatch):
        monkeypatch.delenv("USER_SERVICE_URL", raising=False)
        _, build_headers, _, _ = _import_client()
        h = await build_headers("u", "o", "t")
        assert h["Content-Type"] == "application/json"

    @pytest.mark.asyncio
    async def test_empty_identity_values_accepted(self, monkeypatch):
        monkeypatch.delenv("USER_SERVICE_URL", raising=False)
        _, build_headers, _, _ = _import_client()
        h = await build_headers("", "", "tok")
        assert h["X-User-Id"] == ""
        assert h["X-Org-Id"] == ""


class TestBuildHeadersAccessibleOrgIds:
    """The bug this fixes: a caller who belongs to multiple orgs must see all
    of them in X-Accessible-Org-Ids, not just the session's single org_id —
    workflow-backend's Reader.GetWorkspace filters
    `WHERE organization_id = ANY(accessible)`, so a workspace owned by an org
    the caller belongs to but isn't their "current" session org must still
    resolve, or it 404s as DATABASE_NOT_FOUND despite existing."""

    @pytest.mark.asyncio
    async def test_uses_full_membership_list_not_just_org_id(self, monkeypatch):
        from unittest.mock import AsyncMock

        _, build_headers, _, _ = _import_client()
        monkeypatch.setattr(
            "src.services.workflow_backend_client.get_accessible_org_ids",
            AsyncMock(return_value=["org-kitelabs", "org-inga"]),
        )
        h = await build_headers("uid-1", "org-kitelabs", "svc-token")
        assert h["X-Accessible-Org-Ids"] == "org-kitelabs,org-inga"


# ---------------------------------------------------------------------------
# _extract_reason_code
# ---------------------------------------------------------------------------


class TestExtractReasonCode:
    def test_reason_code_field(self):
        _, _, extract, _ = _import_client()
        assert extract({"reason_code": "tasks_already_exist"}) == "tasks_already_exist"

    def test_reason_field(self):
        _, _, extract, _ = _import_client()
        assert extract({"reason": "feature_not_tasks_approved"}) == "feature_not_tasks_approved"

    def test_error_string_field(self):
        _, _, extract, _ = _import_client()
        assert extract({"error": "tasks_already_exist"}) == "tasks_already_exist"

    def test_nested_error_dict(self):
        _, _, extract, _ = _import_client()
        assert extract({"error": {"code": "tasks_already_exist"}}) == "tasks_already_exist"

    def test_non_dict_body_returns_empty(self):
        _, _, extract, _ = _import_client()
        assert extract("plain string error") == ""
        assert extract(None) == ""
        assert extract([]) == ""

    def test_empty_dict_returns_empty(self):
        _, _, extract, _ = _import_client()
        assert extract({}) == ""


# ---------------------------------------------------------------------------
# create_feature_tasks — async tests
# ---------------------------------------------------------------------------


def _fake_session(fake_resp):
    fake_session = MagicMock()
    fake_session.post = MagicMock(return_value=fake_resp)
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    return fake_session


class TestCreateFeatureTasks:
    @pytest.mark.asyncio
    async def test_missing_url_raises_missing_config(self, monkeypatch):
        WorkflowBackendError, _, _, create = _import_client()
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        with pytest.raises(WorkflowBackendError) as exc_info:
            await create("ws-1", "feat-1", _SAMPLE_TASKS)
        assert exc_info.value.reason_code == "missing_config"

    @pytest.mark.asyncio
    async def test_missing_token_raises_missing_config(self, monkeypatch):
        WorkflowBackendError, _, _, create = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)

        with pytest.raises(WorkflowBackendError) as exc_info:
            await create("ws-1", "feat-1", _SAMPLE_TASKS)
        assert exc_info.value.reason_code == "missing_config"

    @pytest.mark.asyncio
    async def test_empty_task_list_raises_empty_tasks(self, monkeypatch):
        WorkflowBackendError, _, _, create = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        with pytest.raises(WorkflowBackendError) as exc_info:
            await create("ws-1", "feat-1", [])
        assert exc_info.value.reason_code == "empty_tasks"

    @pytest.mark.asyncio
    async def test_success_returns_body(self, monkeypatch):
        _, _, _, create = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        fake_resp = MagicMock()
        fake_resp.status = 201
        fake_resp.json = AsyncMock(return_value={"tasks": [{"name": "T1"}]})
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=_fake_session(fake_resp)),
            patch("plugins.context.get_user_id", return_value="u-1"),
            patch("plugins.context.get_org_id", return_value="o-1"),
        ):
            result = await create("ws-1", "feat-1", _SAMPLE_TASKS)

        assert result == {"tasks": [{"name": "T1"}]}

    @pytest.mark.asyncio
    async def test_4xx_with_reason_code_raises_correct_code(self, monkeypatch):
        WorkflowBackendError, _, _, create = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        fake_resp = MagicMock()
        fake_resp.status = 422
        fake_resp.json = AsyncMock(return_value={"error": "feature_not_tasks_approved"})
        fake_resp.text = AsyncMock(return_value="")
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=_fake_session(fake_resp)),
            patch("plugins.context.get_user_id", return_value="u-1"),
            patch("plugins.context.get_org_id", return_value="o-1"),
        ):
            with pytest.raises(WorkflowBackendError) as exc_info:
                await create("ws-1", "feat-1", _SAMPLE_TASKS)

        assert exc_info.value.reason_code == "feature_not_tasks_approved"
        assert exc_info.value.status == 422

    @pytest.mark.asyncio
    async def test_4xx_tasks_already_exist_surfaces_code(self, monkeypatch):
        WorkflowBackendError, _, _, create = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        fake_resp = MagicMock()
        fake_resp.status = 409
        fake_resp.json = AsyncMock(return_value={"reason_code": "tasks_already_exist"})
        fake_resp.text = AsyncMock(return_value="")
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=_fake_session(fake_resp)),
            patch("plugins.context.get_user_id", return_value="u-1"),
            patch("plugins.context.get_org_id", return_value="o-1"),
        ):
            with pytest.raises(WorkflowBackendError) as exc_info:
                await create("ws-1", "feat-1", _SAMPLE_TASKS)

        assert exc_info.value.reason_code == "tasks_already_exist"

    @pytest.mark.asyncio
    async def test_4xx_without_reason_code_raises_empty_code(self, monkeypatch):
        WorkflowBackendError, _, _, create = _import_client()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "svc-tok")

        fake_resp = MagicMock()
        fake_resp.status = 500
        fake_resp.json = AsyncMock(return_value={"message": "internal server error"})
        fake_resp.text = AsyncMock(return_value="")
        fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
        fake_resp.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("src.services.workflow_backend_client.aiohttp.ClientSession", return_value=_fake_session(fake_resp)),
            patch("plugins.context.get_user_id", return_value="u-1"),
            patch("plugins.context.get_org_id", return_value="o-1"),
        ):
            with pytest.raises(WorkflowBackendError) as exc_info:
                await create("ws-1", "feat-1", _SAMPLE_TASKS)

        assert exc_info.value.reason_code == ""
        assert exc_info.value.status == 500

    @pytest.mark.asyncio
    async def test_headers_contain_identity_from_context(self, monkeypatch):
        """X-User-Id and X-Org-Id are sourced from T1-threaded context getters."""
        _, _, _, create = _import_client()
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
            await create("ws-1", "feat-1", _SAMPLE_TASKS)

        assert captured_headers["X-User-Id"] == "user-123"
        assert captured_headers["X-Org-Id"] == "org-456"
        assert captured_headers["X-Accessible-Org-Ids"] == "org-456"
        assert captured_headers["Authorization"] == "Bearer svc-tok"

    @pytest.mark.asyncio
    async def test_explicit_identity_overrides_context(self, monkeypatch):
        """Identity passed as args is used verbatim, even when context getters are empty.

        Guards the cross-thread bug: the coroutine may run on the agent loop
        thread where thread-local identity is unset, so callers pass it in.
        """
        _, _, _, create = _import_client()
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
            # Context getters return empty — must NOT be used when args are passed.
            patch("plugins.context.get_user_id", return_value=""),
            patch("plugins.context.get_org_id", return_value=""),
        ):
            await create("ws-1", "feat-1", _SAMPLE_TASKS, user_id="passed-user", org_id="passed-org")

        assert captured_headers["X-User-Id"] == "passed-user"
        assert captured_headers["X-Org-Id"] == "passed-org"
        assert captured_headers["X-Accessible-Org-Ids"] == "passed-org"

    @pytest.mark.asyncio
    async def test_payload_contains_supplied_tasks(self, monkeypatch):
        """The POST body must include the supplied tasks verbatim."""
        _, _, _, create = _import_client()
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
            await create("ws-1", "feat-1", _SAMPLE_TASKS)

        tasks = captured_payload.get("tasks", [])
        assert len(tasks) == 5
        assert tasks[0]["name"] == "T1"
        t4 = next(t for t in tasks if t["name"] == "T4")
        assert t4["depends_on"] == ["T1"]

    @pytest.mark.asyncio
    async def test_url_contains_workspace_and_feature(self, monkeypatch):
        """The POST endpoint URL must embed workspace_id and feature_id."""
        _, _, _, create = _import_client()
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
            await create("my-workspace", "my-feature", _SAMPLE_TASKS)

        assert len(captured_url) == 1
        assert "my-workspace" in captured_url[0]
        assert "my-feature" in captured_url[0]


# ---------------------------------------------------------------------------
# _call / run_async / check_workflow_available and the plugins.db replacement
# functions (get_workspace_context, get_feature_detail, update_feature_stage, ...)
# ---------------------------------------------------------------------------


def _import_call_helpers():
    import src.services.workflow_backend_client as mod

    return mod


def _fake_request_session(fake_resp):
    """A fake aiohttp.ClientSession whose .request(...) returns fake_resp."""
    fake_session = MagicMock()
    fake_session.request = MagicMock(return_value=fake_resp)
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    return fake_session


def _fake_request_session_by_url_suffix(by_suffix: dict):
    """A fake aiohttp.ClientSession whose .request(...) returns a different
    fake response depending on the requested URL's suffix — for endpoints
    that make more than one distinct call per invocation."""
    fake_session = MagicMock()

    def _pick(method, url, **_kw):
        for suffix, resp in by_suffix.items():
            if url.endswith(suffix):
                return resp
        raise AssertionError(f"no fake response configured for URL: {url}")

    fake_session.request = MagicMock(side_effect=_pick)
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    return fake_session


def _fake_response(status, body):
    fake_resp = MagicMock()
    fake_resp.status = status
    fake_resp.json = AsyncMock(return_value=body)
    fake_resp.text = AsyncMock(return_value=str(body))
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=False)
    return fake_resp


class TestCheckWorkflowAvailable:
    def test_true_when_both_set(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        assert mod.check_workflow_available() is True

    def test_false_when_url_missing(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")
        assert mod.check_workflow_available() is False

    def test_false_when_token_missing(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
        assert mod.check_workflow_available() is False


class TestRunAsync:
    def test_uses_asyncio_run_when_no_agent_loop(self):
        mod = _import_call_helpers()

        async def coro():
            return 42

        with patch("plugins.context.get_agent_loop", return_value=None):
            assert mod.run_async(coro()) == 42


class TestCall:
    @pytest.mark.asyncio
    async def test_unwraps_success_envelope(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(200, {"success": True, "data": {"slug": "ws-1"}})
        with (
            patch(
                "src.services.workflow_backend_client.aiohttp.ClientSession",
                return_value=_fake_request_session(fake_resp),
            ),
            patch("plugins.context.get_user_id", return_value="u"),
            patch("plugins.context.get_org_id", return_value="o"),
        ):
            result = await mod._call("GET", "/api/workspaces/ws-1", user_id="u", org_id="o")

        assert result == {"slug": "ws-1"}

    @pytest.mark.asyncio
    async def test_404_with_not_found_message_raises_value_error(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(404, {"success": False, "error": {"code": "DATABASE_NOT_FOUND"}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            with pytest.raises(ValueError, match="not found"):
                await mod._call(
                    "GET",
                    "/api/workspaces/missing",
                    user_id="u",
                    org_id="o",
                    not_found_message="Workspace not found: 'missing'",
                )

    @pytest.mark.asyncio
    async def test_404_without_not_found_message_raises_backend_error(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(404, {"success": False, "error": {"code": "DATABASE_NOT_FOUND"}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            with pytest.raises(mod.WorkflowBackendError) as exc_info:
                await mod._call("GET", "/api/workspaces/missing", user_id="u", org_id="o")
        assert exc_info.value.status == 404

    @pytest.mark.asyncio
    async def test_missing_config_raises(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        with pytest.raises(mod.WorkflowBackendError) as exc_info:
            await mod._call("GET", "/api/workspaces/x", user_id="u", org_id="o")
        assert exc_info.value.reason_code == "missing_config"


class TestGetWorkspaceContext:
    @pytest.mark.asyncio
    async def test_merges_full_repo_list_from_repos_endpoint(self, monkeypatch):
        """GET /repos returns every registered repo, not just the legacy
        single repo_url/management_repo_id pair on the workspace detail
        response — get_workspace_context must surface all of them."""
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        workspace_resp = _fake_response(
            200,
            {
                "success": True,
                "data": {
                    "management_repo_id": "mgmt-repo",
                    "repo_url": "https://github.com/org/mgmt-repo",
                },
            },
        )
        repos_resp = _fake_response(
            200,
            {
                "success": True,
                "data": [
                    {
                        "id": "row-1",
                        "repo_id": "mgmt-repo",
                        "repo_url": "https://github.com/org/mgmt-repo",
                        "base_branch": "main",
                        "is_management_repo": True,
                    },
                    {
                        "id": "row-2",
                        "repo_id": "hermes-agent",
                        "repo_url": "https://github.com/org/hermes-agent",
                        "base_branch": "main",
                        "is_management_repo": False,
                    },
                ],
            },
        )
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session_by_url_suffix(
                {"/repos": repos_resp, "/workspaces/ws-1": workspace_resp}
            ),
        ):
            result = await mod.get_workspace_context("ws-1", user_id="u", org_id="o")

        assert result["management_repo"] == "mgmt-repo"
        assert result["repos"] == [
            {"id": "mgmt-repo", "github": "https://github.com/org/mgmt-repo", "base_branch": "main"},
            {"id": "hermes-agent", "github": "https://github.com/org/hermes-agent", "base_branch": "main"},
        ]

    @pytest.mark.asyncio
    async def test_falls_back_to_legacy_repo_url_when_repos_call_fails(self, monkeypatch):
        """If the /repos lookup itself fails, fall back to the single legacy
        repo_url/management_repo_id pair rather than returning nothing."""
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        workspace_resp = _fake_response(
            200,
            {
                "success": True,
                "data": {
                    "management_repo_id": "mgmt-repo",
                    "repo_url": "https://github.com/org/repo",
                },
            },
        )
        repos_resp = _fake_response(500, {"success": False, "error": {"code": "INTERNAL"}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session_by_url_suffix(
                {"/repos": repos_resp, "/workspaces/ws-1": workspace_resp}
            ),
        ):
            result = await mod.get_workspace_context("ws-1", user_id="u", org_id="o")

        assert result == {
            "management_repo": "mgmt-repo",
            "repos": [{"id": "mgmt-repo", "github": "https://github.com/org/repo"}],
        }

    @pytest.mark.asyncio
    async def test_no_repo_url_and_empty_repos_list_gives_empty_repos(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        workspace_resp = _fake_response(
            200, {"success": True, "data": {"management_repo_id": "mgmt-repo", "repo_url": ""}}
        )
        repos_resp = _fake_response(200, {"success": True, "data": []})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session_by_url_suffix(
                {"/repos": repos_resp, "/workspaces/ws-1": workspace_resp}
            ),
        ):
            result = await mod.get_workspace_context("ws-1", user_id="u", org_id="o")

        assert result["repos"] == []


class TestGetWorkspaceOrganizationIdAndSlug:
    @pytest.mark.asyncio
    async def test_organization_id_found(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(200, {"success": True, "data": {"organization_id": "org-1"}})
        fake_session = _fake_request_session(fake_resp)
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=fake_session,
        ):
            assert await mod.get_workspace_organization_id("ws-1", user_id="u", org_id="o") == "org-1"

        # Must hit the unscoped internal service-to-service route, not the
        # user-facing /api/workspaces/:id (which filters by the caller's
        # X-Accessible-Org-Ids and would 404 a real workspace whose owning
        # org isn't already in that list — the exact bug this call exists to
        # resolve in the first place).
        called_url = fake_session.request.call_args.args[1]
        assert called_url == "http://backend:8080/internal/workspaces/ws-1/organization"

    @pytest.mark.asyncio
    async def test_organization_id_none_when_not_found(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(404, {"success": False, "error": {}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            assert await mod.get_workspace_organization_id("ws-1", user_id="u", org_id="o") is None

    @pytest.mark.asyncio
    async def test_slug_empty_when_not_found(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(404, {"success": False, "error": {}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            assert await mod.get_workspace_slug("ws-1", user_id="u", org_id="o") == ""


class TestResolveWorkspaceSlug:
    @pytest.mark.asyncio
    async def test_passthrough_when_unavailable(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        assert await mod.resolve_workspace_slug("ws-1") == "ws-1"

    @pytest.mark.asyncio
    async def test_returns_resolved_slug(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(200, {"success": True, "data": {"slug": "canonical-slug"}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            assert await mod.resolve_workspace_slug("ws-1", user_id="u", org_id="o") == "canonical-slug"

    @pytest.mark.asyncio
    async def test_lookup_miss_falls_back_to_raw_value(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(404, {"success": False, "error": {}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            assert await mod.resolve_workspace_slug("unknown-id", user_id="u", org_id="o") == "unknown-id"

    @pytest.mark.asyncio
    async def test_error_falls_back_to_raw_value(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            side_effect=RuntimeError("connection refused"),
        ):
            assert await mod.resolve_workspace_slug("some-id", user_id="u", org_id="o") == "some-id"


class TestGetWorkspaceIdForFeature:
    @pytest.mark.asyncio
    async def test_returns_workspace_id(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(
            200, {"success": True, "data": {"workspace_id": "ws-1", "feature_id": "feat-1"}}
        )
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            assert await mod.get_workspace_id_for_feature("feat-1", user_id="u", org_id="o") == "ws-1"

    @pytest.mark.asyncio
    async def test_not_found_raises_value_error(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(404, {"success": False, "error": {}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            with pytest.raises(ValueError, match="Feature not found"):
                await mod.get_workspace_id_for_feature("feat-1", user_id="u", org_id="o")


class TestGetFeatureDetailAndTasks:
    @pytest.mark.asyncio
    async def test_get_feature_detail_shapes_response(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(
            200,
            {
                "success": True,
                "data": {
                    "id": "feat-1",
                    "feature_name": "my-feature",
                    "title": "My Feature",
                    "current_stage": "tasks",
                    "status": "ready_for_implementation",
                    "next_action": "write tasks.md",
                    "owner": "go",
                    "init_pr_url": None,
                    "tasks": [],
                    "stages": {"tasks": {"review_status": "pending"}},
                },
            },
        )
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            result = await mod.get_feature_detail("ws-1", "feat-1", user_id="u", org_id="o")

        assert result == {
            "id": "feat-1",
            "feature_name": "my-feature",
            "title": "My Feature",
            "stage": "tasks",
            "status": "ready_for_implementation",
            "next_action": "write tasks.md",
            "owner": "go",
            "init_pr_url": None,
            "stages": {"tasks": {"review_status": "pending"}},
        }

    @pytest.mark.asyncio
    async def test_get_feature_detail_falls_back_to_name_search_on_404(self, monkeypatch):
        """feature_id may be a feature_name slug — workflow-backend's direct
        lookup 404s, so a name-search resolves it to the UUID and retries."""
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        responses = [
            _fake_response(404, {"success": False, "error": {}}),
            _fake_response(
                200,
                {"success": True, "data": {"items": [{"id": "resolved-uuid"}]}},
            ),
            _fake_response(
                200,
                {
                    "success": True,
                    "data": {
                        "id": "resolved-uuid",
                        "feature_name": "my-slug",
                        "title": "My Feature",
                        "current_stage": "tasks",
                        "status": "ready_for_implementation",
                        "next_action": "",
                        "owner": "go",
                        "init_pr_url": None,
                    },
                },
            ),
        ]
        captured_urls = []

        def fake_request(method, url, *, headers, json, timeout):
            captured_urls.append(url)
            return responses[len(captured_urls) - 1]

        fake_session = MagicMock()
        fake_session.request = fake_request
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=fake_session,
        ):
            result = await mod.get_feature_detail("ws-1", "my-slug", user_id="u", org_id="o")

        # The caller passed a slug in, but the resolved "id" must be the
        # canonical UUID — callers that key a storage-service write by this
        # value would otherwise create a duplicate document under the slug.
        assert result["id"] == "resolved-uuid"
        assert result["feature_name"] == "my-slug"
        assert "ws-1/features/my-slug" in captured_urls[0]
        assert "ws-1/features?name=my-slug" in captured_urls[1]
        assert "ws-1/features/resolved-uuid" in captured_urls[2]

    @pytest.mark.asyncio
    async def test_get_feature_detail_raises_when_name_search_also_misses(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        responses = [
            _fake_response(404, {"success": False, "error": {}}),
            _fake_response(200, {"success": True, "data": {"items": []}}),
        ]
        captured_urls = []

        def fake_request(method, url, *, headers, json, timeout):
            captured_urls.append(url)
            return responses[len(captured_urls) - 1]

        fake_session = MagicMock()
        fake_session.request = fake_request
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=fake_session,
        ):
            with pytest.raises(ValueError, match="not found"):
                await mod.get_feature_detail("ws-1", "no-such-feature", user_id="u", org_id="o")

    @pytest.mark.asyncio
    async def test_get_feature_tasks_shapes_response(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(
            200,
            {
                "success": True,
                "data": {
                    "tasks": [
                        {
                            "task_name": "T1",
                            "title": "First task",
                            "status": "ready",
                            "blocked_reason": "",
                            "depends_on": [],
                            "pr": {},
                            "execution": {},
                        }
                    ]
                },
            },
        )
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            result = await mod.get_feature_tasks("ws-1", "feat-1", user_id="u", org_id="o")

        assert result == [
            {
                "task_name": "T1",
                "title": "First task",
                "status": "ready",
                "blocked_reason": "",
                "depends_on": [],
                "pr": {},
                "execution": {},
            }
        ]


class TestResolveFeatureIdByName:
    """Unit tests for _resolve_feature_id_by_name against the new API shape
    (items[0]["id"] rather than items[0]["feature_id"]).
    """

    @pytest.mark.asyncio
    async def test_resolves_id_from_new_api_shape(self, monkeypatch):
        """Returns items[0]["id"] when the search endpoint returns a match."""
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(
            200,
            {"success": True, "data": {"items": [{"id": "abc-123", "feature_name": "my-slug"}]}},
        )
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            result = await mod._resolve_feature_id_by_name(
                "ws-1", "my-slug", user_id="u", org_id="o"
            )

        assert result == "abc-123"

    @pytest.mark.asyncio
    async def test_returns_none_when_items_empty(self, monkeypatch):
        """Returns None when the search returns an empty items list."""
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(
            200, {"success": True, "data": {"items": []}}
        )
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            result = await mod._resolve_feature_id_by_name(
                "ws-1", "no-such-slug", user_id="u", org_id="o"
            )

        assert result is None

    @pytest.mark.asyncio
    async def test_url_encodes_name_with_spaces(self, monkeypatch):
        """The feature name slug is URL-encoded before being appended to the query string."""
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(
            200,
            {"success": True, "data": {"items": [{"id": "xyz-789"}]}},
        )
        captured_urls = []

        fake_session = MagicMock()

        def fake_request(method, url, *, headers, json, timeout):
            captured_urls.append(url)
            return fake_resp

        fake_session.request = fake_request
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=fake_session,
        ):
            result = await mod._resolve_feature_id_by_name(
                "ws-1", "my feature", user_id="u", org_id="o"
            )

        assert result == "xyz-789"
        assert "my+feature" in captured_urls[0] or "my%20feature" in captured_urls[0]


class TestUpdateFeatureStage:
    @pytest.mark.asyncio
    async def test_sends_patch_with_expected_body(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        captured = {}

        def fake_request(method, url, *, headers, json, timeout):
            captured["method"] = method
            captured["url"] = url
            captured["json"] = json
            return _fake_response(200, {"success": True, "data": {"ok": True}})

        fake_session = MagicMock()
        fake_session.request = fake_request
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)

        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=fake_session,
        ):
            await mod.update_feature_stage(
                "ws-1",
                "feat-1",
                "technical_design",
                "approved",
                "ready_for_implementation",
                "tasks",
                "write tasks.md",
                "bob",
                user_id="u",
                org_id="o",
            )

        assert captured["method"] == "PATCH"
        assert "ws-1" in captured["url"]
        assert "feat-1" in captured["url"]
        assert captured["json"]["stage"] == "technical_design"
        assert captured["json"]["actor"] == "bob"

    @pytest.mark.asyncio
    async def test_not_found_raises_value_error(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(404, {"success": False, "error": {}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            with pytest.raises(ValueError):
                await mod.update_feature_stage(
                    "ws-1", "feat-1", "tasks", "approved", "s", "c", "n", "actor", user_id="u", org_id="o"
                )


class TestActivateReadyTasks:
    @pytest.mark.asyncio
    async def test_returns_activated_task_names(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(200, {"success": True, "data": {"activated": ["T1", "T2"]}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            result = await mod.activate_ready_tasks("ws-1", "feat-1", user_id="u", org_id="o")

        assert result == ["T1", "T2"]

    @pytest.mark.asyncio
    async def test_empty_when_nothing_activated(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(200, {"success": True, "data": {"activated": None}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            result = await mod.activate_ready_tasks("ws-1", "feat-1", user_id="u", org_id="o")

        assert result == []

    @pytest.mark.asyncio
    async def test_not_found_raises_value_error(self, monkeypatch):
        mod = _import_call_helpers()
        monkeypatch.setenv("WORKFLOW_BACKEND_URL", "http://backend:8080")
        monkeypatch.setenv("WORKFLOW_BACKEND_SERVICE_TOKEN", "tok")

        fake_resp = _fake_response(404, {"success": False, "error": {}})
        with patch(
            "src.services.workflow_backend_client.aiohttp.ClientSession",
            return_value=_fake_request_session(fake_resp),
        ):
            with pytest.raises(ValueError):
                await mod.activate_ready_tasks("ws-1", "feat-1", user_id="u", org_id="o")
