"""Tests for the vcs_pr_review tool (proxies through vcs-service).

Covers:
  - APPROVE / REQUEST_CHANGES happy path (self_review_skipped=False)
  - self-review path (self_review_skipped=True must not fail the call)
  - vcs-service error propagation (ok=False)
  - Inline comment formatting for comments[] (passed through to the client)
  - check_available(): gates on VCS_SERVICE_URL/VCS_SERVICE_TOKEN presence
  - Tool registered in plugins.__init__._TOOLS with correct name/schema/check_fn
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_PR_URL = "https://github.com/acme/myrepo/pull/42"
_BODY = "## Review\n\nThis looks good overall. 🟢 Nit: rename variable."


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
        fake_response = {
            "review_url": "https://github.com/acme/myrepo/pull/42#pullrequestreview-99",
            "self_review_skipped": False,
        }
        with patch(
            "src.services.vcs_service_client.review_and_comment",
            AsyncMock(return_value=fake_response),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is True
        assert result["self_review_skipped"] is False
        assert result["review_url"] == fake_response["review_url"]

    def test_request_changes_happy_path(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        body = "## Review\n\n🔴 **Blocker** — security issue on line 42."
        fake_response = {"review_url": "https://...", "self_review_skipped": False}
        with patch(
            "src.services.vcs_service_client.review_and_comment",
            AsyncMock(return_value=fake_response),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="REQUEST_CHANGES", body=body)

        assert result["ok"] is True
        assert result["self_review_skipped"] is False

    def test_owner_repo_number_parsed_from_pr_url(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        mock_review = AsyncMock(
            return_value={"review_url": "https://...", "self_review_skipped": False}
        )
        with patch("src.services.vcs_service_client.review_and_comment", mock_review):
            from plugins.tools.vcs_pr_review import handle

            handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert mock_review.call_args.args[:3] == ("acme", "myrepo", 42)

    def test_inline_comments_forwarded(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        comments = [
            {"path": "src/auth.py", "line": 42, "body": "🔴 **Blocker** — SQL injection risk."},
            {"path": "src/utils.py", "line": 10, "body": "🟡 **Warning** — hardcoded timeout."},
        ]
        mock_review = AsyncMock(
            return_value={"review_url": "https://...", "self_review_skipped": False}
        )
        with patch("src.services.vcs_service_client.review_and_comment", mock_review):
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
        mock_review = AsyncMock(
            return_value={"review_url": "https://...", "self_review_skipped": False}
        )
        with patch("src.services.vcs_service_client.review_and_comment", mock_review):
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
        fake_response = {"review_url": comment_url, "self_review_skipped": True}
        with patch(
            "src.services.vcs_service_client.review_and_comment",
            AsyncMock(return_value=fake_response),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is True
        assert result["self_review_skipped"] is True
        assert result["review_url"] == comment_url

    def test_self_review_skipped_regardless_of_event(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        fake_response = {"review_url": "https://...", "self_review_skipped": True}
        with patch(
            "src.services.vcs_service_client.review_and_comment",
            AsyncMock(return_value=fake_response),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="REQUEST_CHANGES", body=_BODY)

        assert result["ok"] is True
        assert result["self_review_skipped"] is True


# ---------------------------------------------------------------------------
# vcs-service error propagation (fatal — comment step failed, or any other
# non-422 failure on the review step)
# ---------------------------------------------------------------------------


class TestErrorPropagation:
    def test_vcs_service_error_returns_ok_false(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        from src.services.vcs_service_client import VCSServiceError

        with patch(
            "src.services.vcs_service_client.review_and_comment",
            AsyncMock(
                side_effect=VCSServiceError(
                    "vcs-service returned HTTP 500 for .../pr/review_and_comment: post issue comment (step 1): boom",
                    status=500,
                )
            ),
        ):
            from plugins.tools.vcs_pr_review import handle

            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is False
        assert "500" in result["error"]

    def test_network_error_returns_ok_false(self, monkeypatch):
        _set_vcs_env(monkeypatch)
        with patch(
            "src.services.vcs_service_client.review_and_comment",
            AsyncMock(side_effect=RuntimeError("connection refused")),
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
        from profiles.workflow.setup import _WORKFLOW_TOOLS
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
