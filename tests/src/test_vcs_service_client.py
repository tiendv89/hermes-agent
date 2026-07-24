"""Tests for the vcs-service client (HTTP only).

Covers:
  - check_vcs_service_available: true/false combinations of URL + token
  - create_pr:
      - missing VCS_SERVICE_URL raises VCSServiceError(missing_config)
      - missing VCS_SERVICE_TOKEN raises VCSServiceError(missing_config)
      - 2xx returns parsed body
      - request payload includes owner/repo/title/head/base
      - optional body/draft included only when truthy
      - Authorization header carries the bearer token
      - 4xx with an "error" message surfaces that message
      - 5xx surfaces status on the raised error
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio  # noqa: F401 — registers the asyncio backend

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

# ---------------------------------------------------------------------------
# Pre-load plugins.context without triggering plugins/__init__.py.
#
# Other test files (e.g. test_suggest_next_actions.py) stub
# sys.modules["plugins.context"] with a bare types.ModuleType() lacking
# get_agent_loop. Since sys.modules is process-global, that stub can leak
# into this file's tests when the full suite runs — re-assert the real
# module before every test so patch("plugins.context.get_agent_loop", ...)
# always resolves.
# ---------------------------------------------------------------------------


def _ensure_plugins_context() -> None:
    existing = sys.modules.get("plugins.context")
    if existing is not None and hasattr(existing, "get_agent_loop"):
        return
    ctx_path = REPO_ROOT / "plugins" / "context.py"
    spec = importlib.util.spec_from_file_location("plugins.context", ctx_path)
    mod = importlib.util.module_from_spec(spec)
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
    _ensure_plugins_context()


def _import_client():
    import src.services.vcs_service_client as mod

    return mod


class _FakeResponse:
    def __init__(self, status: int, body):
        self.status = status
        self._body = body

    async def json(self, content_type=None):
        return self._body

    async def text(self):
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.post_calls = []

    def post(self, url, *, headers=None, json=None, timeout=None):
        self.post_calls.append({"url": url, "headers": headers, "json": json})
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


# ---------------------------------------------------------------------------
# check_vcs_service_available
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    def test_false_when_both_missing(self, monkeypatch):
        monkeypatch.delenv("VCS_SERVICE_URL", raising=False)
        monkeypatch.delenv("VCS_SERVICE_TOKEN", raising=False)
        mod = _import_client()
        assert mod.check_vcs_service_available() is False

    def test_false_when_only_url_set(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.delenv("VCS_SERVICE_TOKEN", raising=False)
        mod = _import_client()
        assert mod.check_vcs_service_available() is False

    def test_false_when_only_token_set(self, monkeypatch):
        monkeypatch.delenv("VCS_SERVICE_URL", raising=False)
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()
        assert mod.check_vcs_service_available() is False

    def test_true_when_both_set(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()
        assert mod.check_vcs_service_available() is True


# ---------------------------------------------------------------------------
# create_pr — misconfiguration
# ---------------------------------------------------------------------------


class TestCreatePRConfig:
    @pytest.mark.asyncio
    async def test_missing_url_raises_missing_config(self, monkeypatch):
        monkeypatch.delenv("VCS_SERVICE_URL", raising=False)
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        with pytest.raises(mod.VCSServiceError) as exc_info:
            await mod.create_pr("acme", "widgets", "Title", "feature", "main")
        assert exc_info.value.reason_code == "missing_config"

    @pytest.mark.asyncio
    async def test_missing_token_raises_missing_config(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.delenv("VCS_SERVICE_TOKEN", raising=False)
        mod = _import_client()

        with pytest.raises(mod.VCSServiceError) as exc_info:
            await mod.create_pr("acme", "widgets", "Title", "feature", "main")
        assert exc_info.value.reason_code == "missing_config"


# ---------------------------------------------------------------------------
# create_pr — request construction + success
# ---------------------------------------------------------------------------


class TestCreatePRRequest:
    @pytest.mark.asyncio
    async def test_success_returns_parsed_body(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_pr = {
            "number": 42,
            "title": "Add retry logic",
            "state": "open",
            "html_url": "https://github.com/acme/widgets/pull/42",
            "head_ref": "feature/retry-logic",
            "base_ref": "main",
        }
        fake_session = _FakeSession(_FakeResponse(201, fake_pr))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            result = await mod.create_pr(
                "acme", "widgets", "Add retry logic", "feature/retry-logic", "main"
            )

        assert result == fake_pr

    @pytest.mark.asyncio
    async def test_payload_includes_required_fields(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(_FakeResponse(201, {"number": 1}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.create_pr("acme", "widgets", "Title", "feature", "main")

        payload = fake_session.post_calls[0]["json"]
        assert payload["owner"] == "acme"
        assert payload["repo"] == "widgets"
        assert payload["title"] == "Title"
        assert payload["head"] == "feature"
        assert payload["base"] == "main"
        assert "body" not in payload
        assert "draft" not in payload

    @pytest.mark.asyncio
    async def test_optional_body_and_draft_included_when_set(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(_FakeResponse(201, {"number": 1}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.create_pr(
                "acme",
                "widgets",
                "Title",
                "feature",
                "main",
                body="Optional description",
                draft=True,
            )

        payload = fake_session.post_calls[0]["json"]
        assert payload["body"] == "Optional description"
        assert payload["draft"] is True

    @pytest.mark.asyncio
    async def test_authorization_header(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "super-secret")
        mod = _import_client()

        fake_session = _FakeSession(_FakeResponse(201, {"number": 1}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.create_pr("acme", "widgets", "Title", "feature", "main")

        headers = fake_session.post_calls[0]["headers"]
        assert headers["Authorization"] == "Bearer super-secret"

    @pytest.mark.asyncio
    async def test_url_join_strips_trailing_slash(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088/")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(_FakeResponse(201, {"number": 1}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.create_pr("acme", "widgets", "Title", "feature", "main")

        assert fake_session.post_calls[0]["url"] == "http://vcs:8088/api/vcs/pr/create"


# ---------------------------------------------------------------------------
# create_pr — error paths
# ---------------------------------------------------------------------------


class TestCreatePRErrors:
    @pytest.mark.asyncio
    async def test_4xx_surfaces_error_message(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(
            _FakeResponse(422, {"error": "owner, repo, title, head, and base are required"})
        )
        with (
            patch("aiohttp.ClientSession", return_value=fake_session),
            pytest.raises(mod.VCSServiceError) as exc_info,
        ):
            await mod.create_pr("acme", "widgets", "Title", "feature", "main")

        assert exc_info.value.status == 422
        assert "required" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_5xx_raises_with_status(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(_FakeResponse(500, {"error": "create PR: internal error"}))
        with (
            patch("aiohttp.ClientSession", return_value=fake_session),
            pytest.raises(mod.VCSServiceError) as exc_info,
        ):
            await mod.create_pr("acme", "widgets", "Title", "feature", "main")

        assert exc_info.value.status == 500


# ---------------------------------------------------------------------------
# ensure_branch
# ---------------------------------------------------------------------------


class TestEnsureBranch:
    @pytest.mark.asyncio
    async def test_success_returns_parsed_body(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(_FakeResponse(200, {"status": "created"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            result = await mod.ensure_branch("acme", "widgets", "feature", "main")

        assert result == {"status": "created"}

    @pytest.mark.asyncio
    async def test_payload_and_url(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(_FakeResponse(200, {"status": "created"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.ensure_branch("acme", "widgets", "feature", "main")

        call = fake_session.post_calls[0]
        assert call["url"] == "http://vcs:8088/api/vcs/repo/ensure_branch"
        assert call["json"] == {
            "owner": "acme",
            "repo": "widgets",
            "branch": "feature",
            "base_branch": "main",
        }

    @pytest.mark.asyncio
    async def test_missing_config_raises(self, monkeypatch):
        monkeypatch.delenv("VCS_SERVICE_URL", raising=False)
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        with pytest.raises(mod.VCSServiceError) as exc_info:
            await mod.ensure_branch("acme", "widgets", "feature", "main")
        assert exc_info.value.reason_code == "missing_config"

    @pytest.mark.asyncio
    async def test_4xx_surfaces_error_message(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(
            _FakeResponse(400, {"error": "owner, repo, branch, and base_branch are required"})
        )
        with (
            patch("aiohttp.ClientSession", return_value=fake_session),
            pytest.raises(mod.VCSServiceError) as exc_info,
        ):
            await mod.ensure_branch("acme", "widgets", "feature", "main")

        assert exc_info.value.status == 400
        assert "required" in str(exc_info.value)


# ---------------------------------------------------------------------------
# commit_files
# ---------------------------------------------------------------------------


class TestCommitFiles:
    @pytest.mark.asyncio
    async def test_success_returns_parsed_body(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(_FakeResponse(200, {"status": "committed"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            result = await mod.commit_files(
                "acme", "widgets", "feature", "test commit", {"README.md": "test"}
            )

        assert result == {"status": "committed"}

    @pytest.mark.asyncio
    async def test_payload_without_base_branch(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(_FakeResponse(200, {"status": "committed"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.commit_files(
                "acme", "widgets", "feature", "test commit", {"README.md": "test"}
            )

        call = fake_session.post_calls[0]
        assert call["url"] == "http://vcs:8088/api/vcs/repo/commit_files"
        assert call["json"] == {
            "owner": "acme",
            "repo": "widgets",
            "branch": "feature",
            "message": "test commit",
            "files": {"README.md": "test"},
        }
        assert "base_branch" not in call["json"]

    @pytest.mark.asyncio
    async def test_base_branch_included_when_set(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(_FakeResponse(200, {"status": "committed"}))
        with patch("aiohttp.ClientSession", return_value=fake_session):
            await mod.commit_files(
                "acme",
                "widgets",
                "feature",
                "test commit",
                {"README.md": "test"},
                base_branch="main",
            )

        assert fake_session.post_calls[0]["json"]["base_branch"] == "main"

    @pytest.mark.asyncio
    async def test_missing_config_raises(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.delenv("VCS_SERVICE_TOKEN", raising=False)
        mod = _import_client()

        with pytest.raises(mod.VCSServiceError) as exc_info:
            await mod.commit_files(
                "acme", "widgets", "feature", "test commit", {"README.md": "test"}
            )
        assert exc_info.value.reason_code == "missing_config"

    @pytest.mark.asyncio
    async def test_4xx_surfaces_error_message(self, monkeypatch):
        monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
        monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")
        mod = _import_client()

        fake_session = _FakeSession(
            _FakeResponse(
                400, {"error": "owner, repo, branch, message, and files are required"}
            )
        )
        with (
            patch("aiohttp.ClientSession", return_value=fake_session),
            pytest.raises(mod.VCSServiceError) as exc_info,
        ):
            await mod.commit_files(
                "acme", "widgets", "feature", "test commit", {"README.md": "test"}
            )

        assert exc_info.value.status == 400
        assert "required" in str(exc_info.value)


# ---------------------------------------------------------------------------
# run_async
# ---------------------------------------------------------------------------


class TestRunAsync:
    def test_falls_back_to_asyncio_run_without_agent_loop(self, monkeypatch):
        mod = _import_client()
        with patch("plugins.context.get_agent_loop", return_value=None):
            async def _coro():
                return "done"

            assert mod.run_async(_coro()) == "done"
