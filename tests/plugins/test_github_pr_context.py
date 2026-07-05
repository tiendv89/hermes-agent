"""Tests for the github_pr_context tool and github_pr_client module.

Covers:
  - check_available(): gates on GITHUB_TOKEN presence
  - handle(): one test per action (happy path + key error paths)
  - get_check_runs(): bounded poll timeout returns 'pending' rather than blocking
  - parse_pr_url(): valid URL parsing and invalid URL rejection
  - Tool registered in plugins.__init__._TOOLS with correct name/schema/check_fn
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

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


# ---------------------------------------------------------------------------
# parse_pr_url
# ---------------------------------------------------------------------------


class TestParsePrUrl:
    def test_valid_url(self):
        from plugins.github_pr_client import parse_pr_url
        owner, repo, num = parse_pr_url("https://github.com/acme/my-repo/pull/42")
        assert owner == "acme"
        assert repo == "my-repo"
        assert num == 42

    def test_valid_url_http(self):
        from plugins.github_pr_client import parse_pr_url
        owner, repo, num = parse_pr_url("http://github.com/org/repo/pull/1")
        assert owner == "org"
        assert repo == "repo"
        assert num == 1

    def test_invalid_url_raises(self):
        from plugins.github_pr_client import parse_pr_url
        with pytest.raises(ValueError, match="Invalid GitHub PR URL"):
            parse_pr_url("https://example.com/not-a-pr")

    def test_missing_pull_number_raises(self):
        from plugins.github_pr_client import parse_pr_url
        with pytest.raises(ValueError):
            parse_pr_url("https://github.com/owner/repo/issues/5")


# ---------------------------------------------------------------------------
# check_available
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    def test_returns_false_when_token_unset(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from plugins.tools.github_pr_context import check_available
        assert check_available() is False

    def test_returns_false_when_token_empty(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "   ")
        from plugins.tools.github_pr_context import check_available
        assert check_available() is False

    def test_returns_true_when_token_set(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test_token")
        from plugins.tools.github_pr_context import check_available
        assert check_available() is True


# ---------------------------------------------------------------------------
# handle() — missing token
# ---------------------------------------------------------------------------


class TestHandleMissingToken:
    def test_returns_error_when_no_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from plugins.tools.github_pr_context import handle
        result = handle(action="metadata", pr_url="https://github.com/a/b/pull/1")
        assert result["ok"] is False
        assert "GITHUB_TOKEN" in result["error"]

    def test_unknown_action_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_context import handle
        result = handle(action="nonexistent")
        assert result["ok"] is False
        assert "nonexistent" in result["error"]


# ---------------------------------------------------------------------------
# handle() — diff action
# ---------------------------------------------------------------------------


class TestHandleDiff:
    def test_happy_path(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        mock_resp = MagicMock()
        mock_resp.text = "diff --git a/foo.py b/foo.py\n+line"
        mock_resp.raise_for_status = MagicMock()
        with patch("plugins.github_pr_client.requests.get", return_value=mock_resp):
            from plugins.tools.github_pr_context import handle
            result = handle(action="diff", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert "diff --git" in result["diff"]

    def test_missing_pr_url(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_context import handle
        result = handle(action="diff")
        assert result["ok"] is False
        assert "pr_url" in result["error"]


# ---------------------------------------------------------------------------
# handle() — files action
# ---------------------------------------------------------------------------


class TestHandleFiles:
    def test_happy_path(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"filename": "a.py", "status": "modified", "additions": 5, "deletions": 2, "changes": 7}
        ]
        mock_resp.raise_for_status = MagicMock()
        with patch("plugins.github_pr_client.requests.get", return_value=mock_resp):
            from plugins.tools.github_pr_context import handle
            result = handle(action="files", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["files"][0]["filename"] == "a.py"
        assert result["files"][0]["status"] == "modified"


# ---------------------------------------------------------------------------
# handle() — metadata action
# ---------------------------------------------------------------------------


class TestHandleMetadata:
    def test_happy_path(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "number": 5,
            "title": "Add feature",
            "body": "description",
            "state": "open",
            "draft": False,
            "user": {"login": "alice"},
            "base": {"ref": "main", "sha": "abc"},
            "head": {"ref": "feat", "sha": "def"},
            "labels": [{"name": "bug"}],
            "requested_reviewers": [{"login": "bob"}],
            "merged": False,
            "merged_at": None,
            "html_url": "https://github.com/o/r/pull/5",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-02T00:00:00Z",
        }
        mock_resp.raise_for_status = MagicMock()
        with patch("plugins.github_pr_client.requests.get", return_value=mock_resp):
            from plugins.tools.github_pr_context import handle
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
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        issue_comment = {
            "id": 1, "user": {"login": "alice"}, "body": "LGTM",
            "created_at": "2026-01-01T00:00:00Z", "html_url": "https://..."
        }
        review_comment = {
            "id": 2, "user": {"login": "bob"}, "body": "Nit",
            "path": "a.py", "line": 10,
            "created_at": "2026-01-01T00:00:00Z", "html_url": "https://..."
        }
        call_count = [0]

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            if "issues" in url:
                resp.json.return_value = [issue_comment]
            else:
                resp.json.return_value = [review_comment]
            call_count[0] += 1
            return resp

        with patch("plugins.github_pr_client.requests.get", side_effect=fake_get):
            from plugins.tools.github_pr_context import handle
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
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {
                "id": 100, "user": {"login": "alice"}, "state": "APPROVED",
                "body": "Looks good", "submitted_at": "2026-01-01T00:00:00Z",
                "html_url": "https://..."
            }
        ]
        mock_resp.raise_for_status = MagicMock()
        with patch("plugins.github_pr_client.requests.get", return_value=mock_resp):
            from plugins.tools.github_pr_context import handle
            result = handle(action="reviews", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["reviews"][0]["state"] == "APPROVED"
        assert result["reviews"][0]["user"] == "alice"


# ---------------------------------------------------------------------------
# handle() — checks action + bounded poll
# ---------------------------------------------------------------------------


class TestHandleChecks:
    def test_happy_path_all_passed(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        # First call returns metadata; second returns check-runs.
        meta_resp = MagicMock()
        meta_resp.raise_for_status = MagicMock()
        meta_resp.json.return_value = {
            "number": 5, "title": "t", "body": "", "state": "open", "draft": False,
            "user": {"login": "u"}, "base": {"ref": "main", "sha": "base"},
            "head": {"ref": "feat", "sha": "abc123"},
            "labels": [], "requested_reviewers": [],
            "merged": False, "merged_at": None, "html_url": "h",
            "created_at": "c", "updated_at": "u",
        }
        checks_resp = MagicMock()
        checks_resp.raise_for_status = MagicMock()
        checks_resp.json.return_value = {
            "check_runs": [
                {"name": "ci", "status": "completed", "conclusion": "success",
                 "html_url": "https://...", "started_at": "s", "completed_at": "c"}
            ]
        }
        responses = iter([meta_resp, checks_resp])

        with patch("plugins.github_pr_client.requests.get", side_effect=lambda *a, **kw: next(responses)):
            from plugins.tools.github_pr_context import handle
            result = handle(action="checks", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["status"] == "passed"
        assert result["check_runs"][0]["conclusion"] == "success"

    def test_poll_timeout_returns_pending(self, monkeypatch):
        """When check-runs are still in progress and timeout expires, status=pending."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        monkeypatch.setenv("CHAT_REVIEW_CI_POLL_TIMEOUT_SECONDS", "0")

        meta_resp = MagicMock()
        meta_resp.raise_for_status = MagicMock()
        meta_resp.json.return_value = {
            "number": 5, "title": "t", "body": "", "state": "open", "draft": False,
            "user": {"login": "u"}, "base": {"ref": "main", "sha": "base"},
            "head": {"ref": "feat", "sha": "sha999"},
            "labels": [], "requested_reviewers": [],
            "merged": False, "merged_at": None, "html_url": "h",
            "created_at": "c", "updated_at": "u",
        }
        checks_resp = MagicMock()
        checks_resp.raise_for_status = MagicMock()
        checks_resp.json.return_value = {
            "check_runs": [
                {"name": "ci", "status": "in_progress", "conclusion": None,
                 "html_url": "https://...", "started_at": "s", "completed_at": None}
            ]
        }
        responses = iter([meta_resp, checks_resp])

        with patch("plugins.github_pr_client.requests.get", side_effect=lambda *a, **kw: next(responses)):
            with patch("plugins.github_pr_client.time.sleep"):  # don't actually sleep
                from plugins.tools.github_pr_context import handle
                result = handle(action="checks", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["status"] == "pending"

    def test_failed_ci(self, monkeypatch):
        """When a check-run has conclusion=failure, status=failed."""
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        meta_resp = MagicMock()
        meta_resp.raise_for_status = MagicMock()
        meta_resp.json.return_value = {
            "number": 5, "title": "t", "body": "", "state": "open", "draft": False,
            "user": {"login": "u"}, "base": {"ref": "main", "sha": "base"},
            "head": {"ref": "feat", "sha": "sha999"},
            "labels": [], "requested_reviewers": [],
            "merged": False, "merged_at": None, "html_url": "h",
            "created_at": "c", "updated_at": "u",
        }
        checks_resp = MagicMock()
        checks_resp.raise_for_status = MagicMock()
        checks_resp.json.return_value = {
            "check_runs": [
                {"name": "ci", "status": "completed", "conclusion": "failure",
                 "html_url": "https://...", "started_at": "s", "completed_at": "c"}
            ]
        }
        responses = iter([meta_resp, checks_resp])
        with patch("plugins.github_pr_client.requests.get", side_effect=lambda *a, **kw: next(responses)):
            from plugins.tools.github_pr_context import handle
            result = handle(action="checks", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["status"] == "failed"

    def test_no_checks_returns_no_checks(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        meta_resp = MagicMock()
        meta_resp.raise_for_status = MagicMock()
        meta_resp.json.return_value = {
            "number": 5, "title": "t", "body": "", "state": "open", "draft": False,
            "user": {"login": "u"}, "base": {"ref": "main", "sha": "base"},
            "head": {"ref": "feat", "sha": "sha000"},
            "labels": [], "requested_reviewers": [],
            "merged": False, "merged_at": None, "html_url": "h",
            "created_at": "c", "updated_at": "u",
        }
        checks_resp = MagicMock()
        checks_resp.raise_for_status = MagicMock()
        checks_resp.json.return_value = {"check_runs": []}
        responses = iter([meta_resp, checks_resp])
        with patch("plugins.github_pr_client.requests.get", side_effect=lambda *a, **kw: next(responses)):
            from plugins.tools.github_pr_context import handle
            result = handle(action="checks", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["status"] == "no_checks"


# ---------------------------------------------------------------------------
# handle() — commits action
# ---------------------------------------------------------------------------


class TestHandleCommits:
    def test_happy_path(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [
            {
                "sha": "abc",
                "commit": {
                    "message": "feat: add thing",
                    "author": {"name": "Alice", "email": "alice@example.com", "date": "2026-01-01T00:00:00Z"},
                },
                "html_url": "https://..."
            }
        ]
        with patch("plugins.github_pr_client.requests.get", return_value=mock_resp):
            from plugins.tools.github_pr_context import handle
            result = handle(action="commits", pr_url="https://github.com/o/r/pull/5")
        assert result["ok"] is True
        assert result["commits"][0]["sha"] == "abc"
        assert result["commits"][0]["message"] == "feat: add thing"


# ---------------------------------------------------------------------------
# handle() — compare action
# ---------------------------------------------------------------------------


class TestHandleCompare:
    def test_happy_path(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "status": "ahead",
            "ahead_by": 3,
            "behind_by": 0,
            "total_commits": 3,
            "commits": [],
            "files": [],
        }
        with patch("plugins.github_pr_client.requests.get", return_value=mock_resp):
            from plugins.tools.github_pr_context import handle
            result = handle(
                action="compare", owner="o", repo="r",
                base="main", head="feat"
            )
        assert result["ok"] is True
        assert result["ahead_by"] == 3
        assert result["status"] == "ahead"

    def test_missing_base_or_head(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_context import handle
        result = handle(action="compare", owner="o", repo="r", base="main")
        assert result["ok"] is False
        assert "base and head" in result["error"]

    def test_missing_owner_repo(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_context import handle
        result = handle(action="compare", base="main", head="feat")
        assert result["ok"] is False
        assert "owner" in result["error"]


# ---------------------------------------------------------------------------
# handle() — file_at_ref action
# ---------------------------------------------------------------------------


class TestHandleFileAtRef:
    def test_happy_path(self, monkeypatch):
        import base64
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        content_b64 = base64.b64encode(b"def hello():\n    pass\n").decode()
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "path": "src/hello.py",
            "sha": "abc",
            "size": 25,
            "encoding": "base64",
            "content": content_b64 + "\n",
            "html_url": "https://...",
        }
        with patch("plugins.github_pr_client.requests.get", return_value=mock_resp):
            from plugins.tools.github_pr_context import handle
            result = handle(
                action="file_at_ref", owner="o", repo="r",
                path="src/hello.py", ref="main"
            )
        assert result["ok"] is True
        assert "def hello" in result["content"]

    def test_missing_path(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_context import handle
        result = handle(action="file_at_ref", owner="o", repo="r", ref="main")
        assert result["ok"] is False
        assert "path" in result["error"]

    def test_missing_ref(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_context import handle
        result = handle(action="file_at_ref", owner="o", repo="r", path="a.py")
        assert result["ok"] is False
        assert "ref" in result["error"]


# ---------------------------------------------------------------------------
# handle() — list_prs action
# ---------------------------------------------------------------------------


class TestHandleListPrs:
    def test_happy_path(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [
            {
                "number": 7, "title": "Open PR", "state": "open", "draft": False,
                "user": {"login": "dev"},
                "base": {"ref": "main"}, "head": {"ref": "feat", "sha": "aaa"},
                "html_url": "https://...", "created_at": "c", "updated_at": "u",
            }
        ]
        with patch("plugins.github_pr_client.requests.get", return_value=mock_resp):
            from plugins.tools.github_pr_context import handle
            result = handle(action="list_prs", owner="o", repo="r")
        assert result["ok"] is True
        assert len(result["pull_requests"]) == 1
        assert result["pull_requests"][0]["number"] == 7

    def test_missing_owner_repo(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_context import handle
        result = handle(action="list_prs")
        assert result["ok"] is False
        assert "owner" in result["error"]


# ---------------------------------------------------------------------------
# API error propagation
# ---------------------------------------------------------------------------


class TestApiErrorPropagation:
    def test_http_error_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from requests import HTTPError
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = HTTPError("404 Not Found")
        with patch("plugins.github_pr_client.requests.get", return_value=mock_resp):
            from plugins.tools.github_pr_context import handle
            result = handle(action="metadata", pr_url="https://github.com/o/r/pull/999")
        assert result["ok"] is False
        assert "404 Not Found" in result["error"]


# ---------------------------------------------------------------------------
# Tool registration in plugins.__init__
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_github_pr_context_registered(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
        monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
        monkeypatch.delenv("RAG_MCP_URL", raising=False)

        # Import the module fresh (autouse fixture clears sys.modules before each test)
        import plugins as plugin_module
        names = {t["name"] for t in plugin_module._TOOLS}
        assert "github_pr_context" in names

    def test_check_fn_gates_on_github_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
        import plugins as plugin_module
        tool = next(t for t in plugin_module._TOOLS if t["name"] == "github_pr_context")
        assert tool["check_fn"]() is False

    def test_check_fn_passes_when_token_set(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
        import plugins as plugin_module
        tool = next(t for t in plugin_module._TOOLS if t["name"] == "github_pr_context")
        assert tool["check_fn"]() is True
