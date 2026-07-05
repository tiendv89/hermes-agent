"""Tests for the github_pr_review tool.

Covers (per tasks.md T2 test plan):
  - APPROVE happy path (HTTP 201 on both calls)
  - REQUEST_CHANGES happy path (HTTP 201 on both calls)
  - HTTP 422 self-review path (tool call must not fail; self_review_skipped=True)
  - Non-422 failure on step 6b (fatal → ok=False)
  - Step 6a (issue comment) failure (fatal → ok=False)
  - Inline comment formatting for comments[] (passed through to step 6b payload)
  - check_available(): gates on GITHUB_TOKEN presence
  - Tool registered in plugins.__init__._TOOLS with correct name/schema/check_fn
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, call

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


def _make_comment_response(html_url: str = "https://github.com/acme/myrepo/issues/42#issuecomment-1") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 201
    resp.json.return_value = {"html_url": html_url, "id": 1}
    resp.raise_for_status = MagicMock()
    return resp


def _make_review_response(status_code: int, html_url: str = "https://github.com/acme/myrepo/pull/42#pullrequestreview-99") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"html_url": html_url, "id": 99}
    resp.text = f"mock response body for {status_code}"
    return resp


# ---------------------------------------------------------------------------
# check_available
# ---------------------------------------------------------------------------


class TestCheckAvailable:
    def test_returns_false_when_token_unset(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from plugins.tools.github_pr_review import check_available
        assert check_available() is False

    def test_returns_false_when_token_empty(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "   ")
        from plugins.tools.github_pr_review import check_available
        assert check_available() is False

    def test_returns_true_when_token_set(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        from plugins.tools.github_pr_review import check_available
        assert check_available() is True


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    def test_missing_pr_url_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_review import handle
        result = handle(event="APPROVE", body=_BODY)
        assert result["ok"] is False
        assert "pr_url" in result["error"]

    def test_missing_body_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_review import handle
        result = handle(pr_url=_PR_URL, event="APPROVE", body="")
        assert result["ok"] is False
        assert "body" in result["error"]

    def test_unknown_event_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_review import handle
        result = handle(pr_url=_PR_URL, event="COMMENT", body=_BODY)
        assert result["ok"] is False
        assert "COMMENT" in result["error"]

    def test_invalid_pr_url_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from plugins.tools.github_pr_review import handle
        result = handle(pr_url="https://example.com/not-a-pr", event="APPROVE", body=_BODY)
        assert result["ok"] is False
        assert "Invalid GitHub PR URL" in result["error"]

    def test_missing_token_returns_error(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        from plugins.tools.github_pr_review import handle
        result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)
        assert result["ok"] is False
        assert "GITHUB_TOKEN" in result["error"]


# ---------------------------------------------------------------------------
# APPROVE happy path
# ---------------------------------------------------------------------------


class TestApproveHappyPath:
    def test_approve_posts_comment_then_review(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        comment_resp = _make_comment_response()
        review_resp = _make_review_response(201)

        post_responses = iter([comment_resp, review_resp])

        with patch("plugins.github_pr_client.requests.post", side_effect=lambda *a, **kw: next(post_responses)):
            from plugins.tools.github_pr_review import handle
            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is True
        assert result["self_review_skipped"] is False
        assert "github.com" in result["review_url"]

    def test_approve_review_url_from_step6b(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        comment_resp = _make_comment_response("https://github.com/acme/myrepo/issues/42#issuecomment-1")
        review_resp = _make_review_response(201, "https://github.com/acme/myrepo/pull/42#pullrequestreview-99")

        post_responses = iter([comment_resp, review_resp])

        with patch("plugins.github_pr_client.requests.post", side_effect=lambda *a, **kw: next(post_responses)):
            from plugins.tools.github_pr_review import handle
            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["review_url"] == "https://github.com/acme/myrepo/pull/42#pullrequestreview-99"


# ---------------------------------------------------------------------------
# REQUEST_CHANGES happy path
# ---------------------------------------------------------------------------


class TestRequestChangesHappyPath:
    def test_request_changes_posts_comment_then_review(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        body = "## Review\n\n🔴 **Blocker** — security issue on line 42."
        comment_resp = _make_comment_response()
        review_resp = _make_review_response(201)

        post_responses = iter([comment_resp, review_resp])

        with patch("plugins.github_pr_client.requests.post", side_effect=lambda *a, **kw: next(post_responses)):
            from plugins.tools.github_pr_review import handle
            result = handle(pr_url=_PR_URL, event="REQUEST_CHANGES", body=body)

        assert result["ok"] is True
        assert result["self_review_skipped"] is False

    def test_request_changes_with_inline_comments(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        comments = [
            {"path": "src/auth.py", "line": 42, "body": "🔴 **Blocker** — SQL injection risk."},
            {"path": "src/utils.py", "line": 10, "body": "🟡 **Warning** — hardcoded timeout."},
        ]
        comment_resp = _make_comment_response()
        review_resp = _make_review_response(201)

        captured_payloads = []

        def fake_post(url, **kwargs):
            captured_payloads.append(kwargs.get("json", {}))
            if "issues" in url:
                return comment_resp
            return review_resp

        with patch("plugins.github_pr_client.requests.post", side_effect=fake_post):
            from plugins.tools.github_pr_review import handle
            result = handle(
                pr_url=_PR_URL,
                event="REQUEST_CHANGES",
                body="Body with findings.",
                comments=comments,
            )

        assert result["ok"] is True
        # Step 6b payload should include the comments array
        review_payload = captured_payloads[1]
        assert "comments" in review_payload
        assert len(review_payload["comments"]) == 2
        assert review_payload["comments"][0]["path"] == "src/auth.py"
        assert review_payload["comments"][1]["line"] == 10


# ---------------------------------------------------------------------------
# HTTP 422 self-review path
# ---------------------------------------------------------------------------


class TestSelfReviewPath:
    def test_422_sets_self_review_skipped_and_does_not_fail(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        comment_url = "https://github.com/acme/myrepo/issues/42#issuecomment-1"
        comment_resp = _make_comment_response(comment_url)
        review_resp = _make_review_response(422)

        post_responses = iter([comment_resp, review_resp])

        with patch("plugins.github_pr_client.requests.post", side_effect=lambda *a, **kw: next(post_responses)):
            from plugins.tools.github_pr_review import handle
            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is True
        assert result["self_review_skipped"] is True
        # review_url falls back to the step-6a comment URL
        assert result["review_url"] == comment_url

    def test_422_does_not_fail_regardless_of_event(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        comment_resp = _make_comment_response()
        review_resp = _make_review_response(422)

        post_responses = iter([comment_resp, review_resp])

        with patch("plugins.github_pr_client.requests.post", side_effect=lambda *a, **kw: next(post_responses)):
            from plugins.tools.github_pr_review import handle
            result = handle(pr_url=_PR_URL, event="REQUEST_CHANGES", body=_BODY)

        assert result["ok"] is True
        assert result["self_review_skipped"] is True


# ---------------------------------------------------------------------------
# Non-422 failure on step 6b (fatal)
# ---------------------------------------------------------------------------


class TestStep6bNon422Fatal:
    def test_500_on_step6b_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        comment_resp = _make_comment_response()
        review_resp = _make_review_response(500)

        post_responses = iter([comment_resp, review_resp])

        with patch("plugins.github_pr_client.requests.post", side_effect=lambda *a, **kw: next(post_responses)):
            from plugins.tools.github_pr_review import handle
            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is False
        assert "500" in result["error"]

    def test_403_on_step6b_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        comment_resp = _make_comment_response()
        review_resp = _make_review_response(403)

        post_responses = iter([comment_resp, review_resp])

        with patch("plugins.github_pr_client.requests.post", side_effect=lambda *a, **kw: next(post_responses)):
            from plugins.tools.github_pr_review import handle
            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is False
        assert "403" in result["error"]


# ---------------------------------------------------------------------------
# Step 6a (issue comment) failure (fatal)
# ---------------------------------------------------------------------------


class TestStep6aFailure:
    def test_http_error_on_comment_post_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        from requests import HTTPError

        bad_resp = MagicMock()
        bad_resp.status_code = 500
        bad_resp.raise_for_status.side_effect = HTTPError(
            "500 Server Error", response=bad_resp
        )

        with patch("plugins.github_pr_client.requests.post", return_value=bad_resp):
            from plugins.tools.github_pr_review import handle
            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is False
        assert "step 6a" in result["error"].lower() or "issue comment" in result["error"].lower()

    def test_network_error_on_comment_post_returns_ok_false(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "tok")
        import requests as req_lib

        with patch("plugins.github_pr_client.requests.post", side_effect=req_lib.ConnectionError("timeout")):
            from plugins.tools.github_pr_review import handle
            result = handle(pr_url=_PR_URL, event="APPROVE", body=_BODY)

        assert result["ok"] is False


# ---------------------------------------------------------------------------
# Tool registration in plugins.__init__
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_github_pr_review_registered(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
        monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
        monkeypatch.delenv("RAG_MCP_URL", raising=False)

        import plugins as plugin_module
        names = {t["name"] for t in plugin_module._TOOLS}
        assert "github_pr_review" in names

    def test_check_fn_gates_on_github_token(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
        import plugins as plugin_module
        tool = next(t for t in plugin_module._TOOLS if t["name"] == "github_pr_review")
        assert tool["check_fn"]() is False

    def test_check_fn_passes_when_token_set(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
        import plugins as plugin_module
        tool = next(t for t in plugin_module._TOOLS if t["name"] == "github_pr_review")
        assert tool["check_fn"]() is True

    def test_schema_has_required_fields(self, monkeypatch):
        monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
        import plugins as plugin_module
        tool = next(t for t in plugin_module._TOOLS if t["name"] == "github_pr_review")
        required = tool["schema"]["parameters"]["required"]
        assert "pr_url" in required
        assert "event" in required
        assert "body" in required

    def test_schema_event_enum(self, monkeypatch):
        monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
        import plugins as plugin_module
        tool = next(t for t in plugin_module._TOOLS if t["name"] == "github_pr_review")
        event_prop = tool["schema"]["parameters"]["properties"]["event"]
        assert "APPROVE" in event_prop["enum"]
        assert "REQUEST_CHANGES" in event_prop["enum"]
