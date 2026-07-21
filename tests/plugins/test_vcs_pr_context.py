"""Tests for the vcs_pr_context tool (proxies through vcs-service).

Covers:
  - check_available(): gates on VCS_SERVICE_URL/VCS_SERVICE_TOKEN presence
  - handle(): one test per action (happy path + key error paths)
  - handle(): missing vcs-service config surfaces as ok=False
  - _parse_pr_url(): valid URL parsing and invalid URL rejection
  - Tool registered in plugins.__init__._TOOLS with correct name/schema/check_fn
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    """Remove cached plugin modules so each test starts fresh."""
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("VCS_SERVICE_URL", raising=False)
    monkeypatch.delenv("VCS_SERVICE_TOKEN", raising=False)
    yield


def _set_vcs_env(monkeypatch):
    monkeypatch.setenv("VCS_SERVICE_URL", "http://vcs:8088")
    monkeypatch.setenv("VCS_SERVICE_TOKEN", "tok")


# ---------------------------------------------------------------------------
# _parse_pr_url
# ---------------------------------------------------------------------------


class TestParsePrUrl:
    def test_valid_url(self):
        from plugins.tools.vcs_pr_context import _parse_pr_url

        owner, repo, num = _parse_pr_url("https://github.com/acme/my-repo/pull/42")
        assert owner == "acme"
        assert repo == "my-repo"
        assert num == 42

    def test_valid_url_http(self):
        from plugins.tools.vcs_pr_context import _parse_pr_url

        owner, repo, num = _parse_pr_url("http://github.com/org/repo/pull/1")
        assert owner == "org"
        assert repo == "repo"
        assert num == 1

    def test_invalid_url_raises(self):
        from plugins.tools.vcs_pr_context import _parse_pr_url

        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            _parse_pr_url("https://example.com/not-a-pr")

    def test_missing_pull_number_raises(self):
        from plugins.tools.vcs_pr_context import _parse_pr_url

        with pytest.raises(ValueError):
            _parse_pr_url("https://github.com/owner/repo/issues/5")


# ---------------------------------------------------------------------------
# check_available
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    def test_returns_false_when_unconfigured(self):
        from plugins.tools.vcs_pr_context import check_available

        assert check_available() is False

    def test_returns_true_when_configured(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_context import check_available

        assert check_available() is True


# ---------------------------------------------------------------------------
# handle() — missing vcs-service config / unknown action
# ---------------------------------------------------------------------------


class TestHandleMissingConfig:
    def test_returns_error_when_unconfigured(self):
        from plugins.tools.vcs_pr_context import handle

        result = handle(action="metadata", pr_url="https://github.com/a/b/pull/1")
        assert result["ok"] is False
        assert "VCS_SERVICE_URL" in result["error"]

    def test_unknown_action_returns_error(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_context import handle

        result = handle(action="nonexistent")
        assert result["ok"] is False
        assert "nonexistent" in result["error"]


# ---------------------------------------------------------------------------
# handle() — diff action
# ---------------------------------------------------------------------------


class TestHandleDiff:
    def test_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        with patch(
            "src.services.vcs_service_client.get_pr_diff",
            AsyncMock(return_value="diff --git a/foo.py b/foo.py\n+line"),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="diff", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert "diff --git" in result["diff"]

    def test_missing_pr_url(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_context import handle

        result = handle(action="diff")
        assert result["ok"] is False
        assert "pr_url" in result["error"]


# ---------------------------------------------------------------------------
# handle() — files action
# ---------------------------------------------------------------------------


class TestHandleFiles:
    def test_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_files = {
            "files": [
                {"filename": "a.py", "status": "modified", "additions": 5, "deletions": 2, "changes": 7}
            ]
        }
        with patch(
            "src.services.vcs_service_client.get_pr_files", AsyncMock(return_value=fake_files)
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="files", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["files"][0]["filename"] == "a.py"
        assert result["files"][0]["status"] == "modified"


# ---------------------------------------------------------------------------
# handle() — metadata action
# ---------------------------------------------------------------------------


class TestHandleMetadata:
    def test_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_metadata = {
            "number": 5,
            "title": "Add feature",
            "body": "description",
            "state": "open",
            "draft": False,
            "author": "alice",
            "base_branch": "main",
            "base_sha": "abc",
            "head_branch": "feat",
            "head_sha": "def",
            "labels": ["bug"],
            "requested_reviewers": ["bob"],
            "merged": False,
            "merged_at": None,
            "html_url": "https://github.com/o/r/pull/5",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
        with patch(
            "src.services.vcs_service_client.get_pr_metadata",
            AsyncMock(return_value=fake_metadata),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="metadata", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        m = result["metadata"]
        assert m["title"] == "Add feature"
        assert m["author"] == "alice"
        assert m["labels"] == ["bug"]
        assert m["requested_reviewers"] == ["bob"]


# ---------------------------------------------------------------------------
# handle() — comments action
# ---------------------------------------------------------------------------


class TestHandleComments:
    def test_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_comments = {
            "issue_comments": [
                {"id": 1, "user": "alice", "body": "LGTM", "created_at": "2026-01-01T00:00:00Z", "html_url": "https://..."}
            ],
            "review_comments": [
                {"id": 2, "user": "bob", "body": "Nit", "path": "a.py", "line": 10,
                 "created_at": "2026-01-01T00:00:00Z", "html_url": "https://..."}
            ],
        }
        with patch(
            "src.services.vcs_service_client.get_pr_comments",
            AsyncMock(return_value=fake_comments),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="comments", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert len(result["issue_comments"]) == 1
        assert result["issue_comments"][0]["body"] == "LGTM"
        assert len(result["review_comments"]) == 1
        assert result["review_comments"][0]["path"] == "a.py"


# ---------------------------------------------------------------------------
# handle() — reviews action
# ---------------------------------------------------------------------------


class TestHandleReviews:
    def test_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_reviews = {
            "reviews": [
                {"id": 100, "user": "alice", "state": "APPROVED", "body": "Looks good",
                 "submitted_at": "2026-01-01T00:00:00Z", "html_url": "https://..."}
            ]
        }
        with patch(
            "src.services.vcs_service_client.get_pr_reviews",
            AsyncMock(return_value=fake_reviews),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="reviews", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["reviews"][0]["state"] == "APPROVED"
        assert result["reviews"][0]["user"] == "alice"


# ---------------------------------------------------------------------------
# handle() — checks action
# ---------------------------------------------------------------------------


class TestHandleChecks:
    def test_happy_path_all_passed(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_metadata = {"head_sha": "abc123"}
        fake_checks = {
            "status": "passed",
            "check_runs": [
                {"name": "ci", "status": "completed", "conclusion": "success",
                 "html_url": "https://...", "started_at": "s", "completed_at": "c"}
            ],
        }
        with patch(
            "src.services.vcs_service_client.get_pr_metadata",
            AsyncMock(return_value=fake_metadata),
        ), patch(
            "src.services.vcs_service_client.get_check_runs",
            AsyncMock(return_value=fake_checks),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="checks", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["status"] == "passed"
        assert result["check_runs"][0]["conclusion"] == "success"

    def test_missing_head_sha_returns_error(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        with patch(
            "src.services.vcs_service_client.get_pr_metadata",
            AsyncMock(return_value={"head_sha": ""}),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="checks", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is False
        assert "head SHA" in result["error"]

    def test_poll_timeout_forwarded_from_env(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        monkeypatch.setenv("CHAT_REVIEW_CI_POLL_TIMEOUT_SECONDS", "5")
        mock_checks = AsyncMock(return_value={"status": "pending", "check_runs": []})
        with patch(
            "src.services.vcs_service_client.get_pr_metadata",
            AsyncMock(return_value={"head_sha": "sha999"}),
        ), patch("src.services.vcs_service_client.get_check_runs", mock_checks):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="checks", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert mock_checks.call_args.kwargs["poll_timeout_seconds"] == 5

    def test_failed_ci(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        with patch(
            "src.services.vcs_service_client.get_pr_metadata",
            AsyncMock(return_value={"head_sha": "sha999"}),
        ), patch(
            "src.services.vcs_service_client.get_check_runs",
            AsyncMock(return_value={"status": "failed", "check_runs": []}),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="checks", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["status"] == "failed"

    def test_no_checks_returns_no_checks(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        with patch(
            "src.services.vcs_service_client.get_pr_metadata",
            AsyncMock(return_value={"head_sha": "sha000"}),
        ), patch(
            "src.services.vcs_service_client.get_check_runs",
            AsyncMock(return_value={"status": "no_checks", "check_runs": []}),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="checks", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["status"] == "no_checks"


# ---------------------------------------------------------------------------
# handle() — commits action
# ---------------------------------------------------------------------------


class TestHandleCommits:
    def test_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_commits = {
            "commits": [
                {"sha": "abc", "message": "feat: add thing", "author": "Alice",
                 "author_email": "alice@example.com", "date": "2026-01-01T00:00:00Z",
                 "html_url": "https://..."}
            ]
        }
        with patch(
            "src.services.vcs_service_client.get_pr_commits",
            AsyncMock(return_value=fake_commits),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="commits", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["commits"][0]["sha"] == "abc"
        assert result["commits"][0]["message"] == "feat: add thing"


# ---------------------------------------------------------------------------
# handle() — compare action
# ---------------------------------------------------------------------------


class TestHandleCompare:
    def test_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_compare = {
            "status": "ahead", "ahead_by": 3, "behind_by": 0, "total_commits": 3,
            "commits": [], "files": [],
        }
        with patch(
            "src.services.vcs_service_client.compare_refs",
            AsyncMock(return_value=fake_compare),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="compare", owner="o", repo="r", base="main", head="feat")
        assert result["ok"] is True
        assert result["ahead_by"] == 3
        assert result["status"] == "ahead"

    def test_missing_base_or_head(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_context import handle

        result = handle(action="compare", owner="o", repo="r", base="main")
        assert result["ok"] is False
        assert "base and head" in result["error"]

    def test_missing_owner_repo(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_context import handle

        result = handle(action="compare", base="main", head="feat")
        assert result["ok"] is False
        assert "owner" in result["error"]


# ---------------------------------------------------------------------------
# handle() — file_at_ref action
# ---------------------------------------------------------------------------


class TestHandleFileAtRef:
    def test_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_file = {
            "path": "src/hello.py", "sha": "abc", "size": 25,
            "content": "def hello():\n    pass\n",
        }
        with patch(
            "src.services.vcs_service_client.get_file_at_ref",
            AsyncMock(return_value=fake_file),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="file_at_ref", owner="o", repo="r", path="src/hello.py", ref="main")
        assert result["ok"] is True
        assert "def hello" in result["content"]

    def test_missing_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_context import handle

        result = handle(action="file_at_ref", owner="o", repo="r", ref="main")
        assert result["ok"] is False
        assert "path" in result["error"]

    def test_missing_ref(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_context import handle

        result = handle(action="file_at_ref", owner="o", repo="r", path="a.py")
        assert result["ok"] is False
        assert "ref" in result["error"]


# ---------------------------------------------------------------------------
# handle() — list_prs action
# ---------------------------------------------------------------------------


class TestHandleListPrs:
    def test_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_list = {
            "prs": [
                {"number": 7, "title": "Open PR", "state": "open", "draft": False,
                 "author": "dev", "base_branch": "main", "head_branch": "feat",
                 "head_sha": "aaa", "html_url": "https://...", "created_at": "c", "updated_at": "u"}
            ]
        }
        with patch(
            "src.services.vcs_service_client.list_prs", AsyncMock(return_value=fake_list)
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="list_prs", owner="o", repo="r")
        assert result["ok"] is True
        assert len(result["pull_requests"]) == 1
        assert result["pull_requests"][0]["number"] == 7

    def test_missing_owner_repo(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_context import handle

        result = handle(action="list_prs")
        assert result["ok"] is False
        assert "owner" in result["error"]


# ---------------------------------------------------------------------------
# API error propagation
# ---------------------------------------------------------------------------


class TestApiErrorPropagation:
    def test_vcs_service_error_returns_ok_false(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from src.services.vcs_service_client import VCSServiceError

        with patch(
            "src.services.vcs_service_client.get_pr_metadata",
            AsyncMock(side_effect=VCSServiceError("vcs-service returned HTTP 404: Not Found", status=404)),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="metadata", pr_url="https://github.com/o/r/pull/999")
        assert result["ok"] is False
        assert "404" in result["error"]

    def test_network_error_returns_ok_false(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        with patch(
            "src.services.vcs_service_client.get_pr_metadata",
            AsyncMock(side_effect=RuntimeError("connection refused")),
        ):
            from plugins.tools.vcs_pr_context import handle

            result = handle(action="metadata", pr_url="https://github.com/o/r/pull/999")
        assert result["ok"] is False
        assert "connection refused" in result["error"]


# ---------------------------------------------------------------------------
# Tool registration in plugins.__init__
# ---------------------------------------------------------------------------


class TestToolRegistration:
    @staticmethod
    def _get_tools():
        """Return the workflow tool list from the profile setup module."""
        from profiles.workflow.setup import _WORKFLOW_TOOLS
        return _WORKFLOW_TOOLS

    def test_vcs_pr_context_registered(self, monkeypatch):
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
        monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
        monkeypatch.delenv("RAG_MCP_URL", raising=False)

        names = {t["name"] for t in self._get_tools()}
        assert "vcs_pr_context" in names

    def test_check_fn_gates_on_vcs_service_config(self, monkeypatch):
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)

        tool = next(t for t in self._get_tools() if t["name"] == "vcs_pr_context")
        assert tool["check_fn"]() is False

    def test_check_fn_passes_when_configured(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)

        tool = next(t for t in self._get_tools() if t["name"] == "vcs_pr_context")
        assert tool["check_fn"]() is True
