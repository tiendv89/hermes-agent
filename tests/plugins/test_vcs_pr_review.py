"""Tests for the vcs_pr_review tool (proxies through vcs-service).

Covers:
  - APPROVE / REQUEST_CHANGES happy path (self_review_skipped=False)
  - self-review path (self_review_skipped=True must not fail the call)
  - error propagation (ok=False) for both step 6a and step 6b
  - Inline comment formatting for comments[] (passed through to the client)
  - check_available(): gates on VCS_SERVICE_URL/VCS_SERVICE_TOKEN presence
  - Tool registered in plugins.__init__._TOOLS with correct name/schema/check_fn

Note: handle() implements the two-call pattern itself — it calls
plugins.clients.vcs_client.post_issue_comment (step 6a) then .post_pr_review
(step 6b) directly, both imported by name into vcs_pr_review's own module
namespace. So mocks must patch ``plugins.tools.vcs_pr_review.<name>``. There
is no single "review_and_comment" call to mock. post_pr_review returns a raw
requests.Response-like object (handle() reads .status_code / .json() / .text),
so its mock needs those attributes rather than a plain return dict.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_PR_URL = "https://github.com/acme/myrepo/pull/42"
_BODY = "## Review\n\nThis looks good overall. \U0001f7e2 Nit: rename variable."


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


def _mock_review_response(status_code, *, html_url="", text=""):
    resp = Mock(spec=requests.Response)
    resp.status_code = status_code
    resp.json.return_value = {"html_url": html_url}
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# check_available
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    def test_returns_false_when_unconfigured(self):
        from plugins.tools.vcs_pr_review import check_available

        assert check_available() is False

    def test_returns_true_when_configured(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_review import check_available

        assert check_available() is True


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_missing_pr_url_returns_error(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_review import handle

        result = handle(event="APPROVE", body=_BODY)
        assert result["ok"] is False
        assert "pr_url" in result["error"]

    def test_missing_body_returns_error(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_review import handle

        result = handle(pr_url=_PR_URL, event="APPROVE", body="")
        assert result["ok"] is False
        assert "body" in result["error"]

    def test_unknown_event_returns_error(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_review import handle

        result = handle(pr_url=_PR_URL, event="COMMENT", body=_BODY)
        assert result["ok"] is False
        assert "COMMENT" in result["error"]

    def test_invalid_pr_url_returns_error(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from plugins.tools.vcs_pr_review import handle

        result = handle(pr_url="https://example.com/not-a-pr", event="APPROVE", body=_BODY)
        assert result["ok"] is False
        assert "Invalid GitHub PR URL" in result["error"]

    def test_missing_vcs_service_config_returns_error(self):
        from plugins.tools.vcs_pr_review import handle

        result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)
        assert result["ok"] is False
        assert "VCS_SERVICE_URL" in result["error"]


# ---------------------------------------------------------------------------
# APPROVE / REQUEST_CHANGES happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_approve_returns_review_url_and_not_skipped(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        review_url = "https://github.com/acme/myrepo/pull/42#pullrequestreview-99"
        with patch(
            "plugins.tools.vcs_pr_review.post_issue_comment",
            Mock(return_value={"html_url": "https://.../issuecomment-1"}),
        ), patch(
            "plugins.tools.vcs_pr_review.post_pr_review",
            Mock(return_value=_mock_review_response(201, html_url=review_url)),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is True
        assert result["self_review_skipped"] is False
        assert result["review_url"] == review_url

    def test_request_changes_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        body = "## Review\n\n\U0001f534 **Blocker** — security issue on line 42."
        with patch(
            "plugins.tools.vcs_pr_review.post_issue_comment",
            Mock(return_value={"html_url": "https://.../issuecomment-1"}),
        ), patch(
            "plugins.tools.vcs_pr_review.post_pr_review",
            Mock(return_value=_mock_review_response(201, html_url="https://...")),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="REQUEST_CHANGES", body=body)

        assert result["ok"] is True
        assert result["self_review_skipped"] is False

    def test_owner_repo_number_parsed_from_pr_url(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        mock_review = Mock(return_value=_mock_review_response(201, html_url="https://..."))
        with patch(
            "plugins.tools.vcs_pr_review.post_issue_comment",
            Mock(return_value={"html_url": "https://.../issuecomment-1"}),
        ), patch("plugins.tools.vcs_pr_review.post_pr_review", mock_review):
            from plugins.tools.vcs_pr_review import handle

            handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert mock_review.call_args.args[:3] == ("acme", "myrepo", 42)

    def test_inline_comments_forwarded(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        comments = [
            {"path": "src/auth.py", "line": 42, "body": "\U0001f534 **Blocker** — SQL injection risk."},
            {"path": "src/utils.py", "line": 10, "body": "\U0001f7e1 **Warning** — hardcoded timeout."},
        ]
        mock_review = Mock(return_value=_mock_review_response(201, html_url="https://..."))
        with patch(
            "plugins.tools.vcs_pr_review.post_issue_comment",
            Mock(return_value={"html_url": "https://.../issuecomment-1"}),
        ), patch("plugins.tools.vcs_pr_review.post_pr_review", mock_review):
            from plugins.tools.vcs_pr_review import handle

            result = handle(
                pr_url=_PR_URL, event="REQUEST_CHANGES", body="Body with findings.",
                comments=comments,
            )

        assert result["ok"] is True
        forwarded = mock_review.call_args.kwargs["comments"]
        assert len(forwarded) == 2
        assert forwarded[0]["path"] == "src/auth.py"
        assert forwarded[1]["line"] == 10

    def test_no_comments_forwards_empty_list(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        mock_review = Mock(return_value=_mock_review_response(201, html_url="https://..."))
        with patch(
            "plugins.tools.vcs_pr_review.post_issue_comment",
            Mock(return_value={"html_url": "https://.../issuecomment-1"}),
        ), patch("plugins.tools.vcs_pr_review.post_pr_review", mock_review):
            from plugins.tools.vcs_pr_review import handle

            handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert mock_review.call_args.kwargs["comments"] == []


# ---------------------------------------------------------------------------
# Self-review path
# ---------------------------------------------------------------------------


class TestSelfReviewPath:
    def test_self_review_skipped_does_not_fail(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        comment_url = "https://github.com/acme/myrepo/issues/42#issuecomment-1"
        with patch(
            "plugins.tools.vcs_pr_review.post_issue_comment",
            Mock(return_value={"html_url": comment_url}),
        ), patch(
            "plugins.tools.vcs_pr_review.post_pr_review",
            Mock(return_value=_mock_review_response(422, text="self-review not allowed")),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is True
        assert result["self_review_skipped"] is True
        assert result["review_url"] == comment_url

    def test_self_review_skipped_regardless_of_event(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        with patch(
            "plugins.tools.vcs_pr_review.post_issue_comment",
            Mock(return_value={"html_url": "https://.../issuecomment-1"}),
        ), patch(
            "plugins.tools.vcs_pr_review.post_pr_review",
            Mock(return_value=_mock_review_response(422, text="self-review not allowed")),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="REQUEST_CHANGES", body=_BODY)

        assert result["ok"] is True
        assert result["self_review_skipped"] is True


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    def test_step6a_http_error_returns_ok_false(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_response = Mock(spec=requests.Response)
        fake_response.status_code = 500
        http_error = requests.HTTPError("500 Server Error")
        http_error.response = fake_response
        with patch(
            "plugins.tools.vcs_pr_review.post_issue_comment",
            Mock(side_effect=http_error),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is False
        assert "500" in result["error"]
        assert "step 6a" in result["error"]

    def test_step6b_non422_failure_returns_ok_false(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        with patch(
            "plugins.tools.vcs_pr_review.post_issue_comment",
            Mock(return_value={"html_url": "https://.../issuecomment-1"}),
        ), patch(
            "plugins.tools.vcs_pr_review.post_pr_review",
            Mock(return_value=_mock_review_response(500, text="boom")),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is False
        assert "500" in result["error"]
        assert "step 6b" in result["error"]

    def test_network_error_returns_ok_false(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        with patch(
            "plugins.tools.vcs_pr_review.post_issue_comment",
            Mock(side_effect=RuntimeError("connection refused")),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is False
        assert "connection refused" in result["error"]


# ---------------------------------------------------------------------------
# Tool registration in plugins.__init__
# ---------------------------------------------------------------------------


class TestToolRegistration:
    @staticmethod
    def _get_tools():
        """Return the workflow tool list from the profile setup module."""
        from src.tool_setup import _WORKFLOW_TOOLS
        return _WORKFLOW_TOOLS

    def test_vcs_pr_review_registered(self, monkeypatch):
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
        monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
        monkeypatch.delenv("RAG_MCP_URL", raising=False)

        names = {t["name"] for t in self._get_tools()}
        assert "vcs_pr_review" in names

    def test_check_fn_gates_on_vcs_service_config(self, monkeypatch):
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)

        tool = next(t for t in self._get_tools() if t["name"] == "vcs_pr_review")
        assert tool["check_fn"]() is False

    def test_check_fn_passes_when_configured(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
        monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)

        tool = next(t for t in self._get_tools() if t["name"] == "vcs_pr_review")
        assert tool["check_fn"]() is True

    def test_schema_has_required_fields(self):
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_pr_review")
        required = tool["schema"]["parameters"]["required"]
        assert "pr_url" in required
        assert "event" in required
        assert "body" in required

    def test_schema_event_enum(self):
        tool = next(t for t in self._get_tools() if t["name"] == "vcs_pr_review")
        event_prop = tool["schema"]["parameters"]["properties"]["event"]
        assert "APPROVE" in event_prop["enum"]
        assert "REQUEST_CHANGES" in event_prop["enum"]
