"""Tests for document branch resolution, branch_exists, and commit_to_branch.

Covers:
  - _resolve_document_branch: init PR open, init PR merged, no init PR
  - document_repo.branch_exists: present (200) and absent (404)
  - document_repo.commit_to_branch: happy path, stale SHA (409)
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# _resolve_document_branch
# ---------------------------------------------------------------------------


class TestResolveDocumentBranch:
    def test_init_pr_open_returns_init_branch(self, monkeypatch):
        """init_pr_url set + init branch exists → use init PR branch."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        with patch(
            "plugins.tools.artifacts.branch_exists", return_value=True
        ) as mock_exists:
            from plugins.tools.artifacts import _resolve_document_branch

            branch, pr_url = _resolve_document_branch(
                "org",
                "repo",
                "feat-1",
                "https://github.com/org/repo/pull/42",
                "main",
                "ghp_test",
            )
        assert branch == "feature/feat-1-init"
        assert pr_url == "https://github.com/org/repo/pull/42"
        mock_exists.assert_called_once_with(
            "org", "repo", "feature/feat-1-init", "ghp_test"
        )

    def test_init_pr_merged_returns_feature_branch(self, monkeypatch):
        """init_pr_url set but branch gone (merged) → use feature/<id> branch."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        with (
            patch("plugins.tools.artifacts.branch_exists", return_value=False),
            patch("plugins.tools.artifacts.ensure_feature_branch"),
        ):
            from plugins.tools.artifacts import _resolve_document_branch

            branch, pr_url = _resolve_document_branch(
                "org",
                "repo",
                "feat-1",
                "https://github.com/org/repo/pull/42",
                "main",
                "ghp_test",
            )
        assert branch == "feature/feat-1"
        assert pr_url is None

    def test_no_init_pr_returns_feature_branch(self, monkeypatch):
        """init_pr_url is None (pre-existing feature) → use feature/<id> directly."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        with patch("plugins.tools.artifacts.ensure_feature_branch") as mock_ensure:
            from plugins.tools.artifacts import _resolve_document_branch

            branch, pr_url = _resolve_document_branch(
                "org",
                "repo",
                "feat-1",
                None,
                "main",
                "ghp_test",
            )
        assert branch == "feature/feat-1"
        assert pr_url is None
        mock_ensure.assert_called_once_with("org", "repo", "feat-1", "main", "ghp_test")

    def test_no_init_pr_does_not_call_branch_exists(self, monkeypatch):
        """When init_pr_url is None, branch_exists should not be called."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        with (
            patch("plugins.tools.artifacts.branch_exists") as mock_exists,
            patch("plugins.tools.artifacts.ensure_feature_branch"),
        ):
            from plugins.tools.artifacts import _resolve_document_branch

            _resolve_document_branch("org", "repo", "feat-1", None, "main", "ghp_test")
        mock_exists.assert_not_called()


# ---------------------------------------------------------------------------
# branch_exists in document_repo
# ---------------------------------------------------------------------------


class TestBranchExists:
    def test_returns_true_when_branch_present(self, requests_mock):
        requests_mock.get(
            "https://api.github.com/repos/org/repo/git/refs/heads/feature/feat-1-init",
            json={"ref": "refs/heads/feature/feat-1-init"},
            status_code=200,
        )
        from plugins.document_repo import branch_exists

        assert branch_exists("org", "repo", "feature/feat-1-init", "ghp_test") is True

    def test_returns_false_when_branch_absent(self, requests_mock):
        requests_mock.get(
            "https://api.github.com/repos/org/repo/git/refs/heads/feature/feat-1-init",
            status_code=404,
        )
        from plugins.document_repo import branch_exists

        assert branch_exists("org", "repo", "feature/feat-1-init", "ghp_test") is False

    def test_raises_on_non_404_error(self, requests_mock):
        requests_mock.get(
            "https://api.github.com/repos/org/repo/git/refs/heads/feature/feat-1-init",
            status_code=403,
        )
        from plugins.document_repo import branch_exists

        with pytest.raises(Exception):
            branch_exists("org", "repo", "feature/feat-1-init", "ghp_test")


# ---------------------------------------------------------------------------
# commit_to_branch in document_repo
# ---------------------------------------------------------------------------


class TestCommitToBranch:
    def test_happy_path_returns_commit_sha(self, requests_mock):
        requests_mock.put(
            "https://api.github.com/repos/org/repo/contents/docs/features/feat-1/product-spec.md",
            json={"commit": {"sha": "deadbeef"}},
            status_code=200,
        )
        from plugins.document_repo import commit_to_branch

        sha = commit_to_branch(
            "org",
            "repo",
            "feature/feat-1-init",
            "docs/features/feat-1/product-spec.md",
            "# New content",
            "existingsha",
            "docs: update product spec",
            "ghp_test",
        )
        assert sha == "deadbeef"

    def test_new_file_without_sha(self, requests_mock):
        requests_mock.put(
            "https://api.github.com/repos/org/repo/contents/docs/features/feat-1/product-spec.md",
            json={"commit": {"sha": "newfile123"}},
            status_code=201,
        )
        from plugins.document_repo import commit_to_branch

        sha = commit_to_branch(
            "org",
            "repo",
            "feature/feat-1-init",
            "docs/features/feat-1/product-spec.md",
            "# Brand new",
            None,
            "docs: initial product spec",
            "ghp_test",
        )
        assert sha == "newfile123"
        # sha should NOT be in the PUT body when base_sha is None
        body = json.loads(requests_mock.last_request.text)
        assert "sha" not in body

    def test_stale_sha_raises_stale_base_error_409(self, requests_mock):
        requests_mock.put(
            "https://api.github.com/repos/org/repo/contents/docs/features/feat-1/product-spec.md",
            json={"message": "does not match"},
            status_code=409,
        )
        from plugins.document_repo import StaleBaseError, commit_to_branch

        with pytest.raises(StaleBaseError):
            commit_to_branch(
                "org",
                "repo",
                "feature/feat-1-init",
                "docs/features/feat-1/product-spec.md",
                "# Content",
                "stalesha",
                "docs: update",
                "ghp_test",
            )

    def test_stale_sha_raises_stale_base_error_422(self, requests_mock):
        requests_mock.put(
            "https://api.github.com/repos/org/repo/contents/docs/features/feat-1/product-spec.md",
            json={"message": "sha conflict"},
            status_code=422,
        )
        from plugins.document_repo import StaleBaseError, commit_to_branch

        with pytest.raises(StaleBaseError):
            commit_to_branch(
                "org",
                "repo",
                "feature/feat-1-init",
                "docs/features/feat-1/product-spec.md",
                "# Content",
                "stalesha",
                "docs: update",
                "ghp_test",
            )

    def test_content_is_base64_encoded(self, requests_mock):
        requests_mock.put(
            "https://api.github.com/repos/org/repo/contents/docs/features/feat-1/product-spec.md",
            json={"commit": {"sha": "abc"}},
            status_code=200,
        )
        from plugins.document_repo import commit_to_branch

        raw = "# Product Spec\n\nWith unicode: café"
        commit_to_branch(
            "org",
            "repo",
            "feature/feat-1-init",
            "docs/features/feat-1/product-spec.md",
            raw,
            "existingsha",
            "docs: update",
            "ghp_test",
        )
        body = json.loads(requests_mock.last_request.text)
        decoded = base64.b64decode(body["content"]).decode("utf-8")
        assert decoded == raw

    def test_correct_branch_in_payload(self, requests_mock):
        requests_mock.put(
            "https://api.github.com/repos/org/repo/contents/docs/features/feat-1/product-spec.md",
            json={"commit": {"sha": "abc"}},
            status_code=200,
        )
        from plugins.document_repo import commit_to_branch

        commit_to_branch(
            "org",
            "repo",
            "feature/feat-1-init",
            "docs/features/feat-1/product-spec.md",
            "# x",
            "existingsha",
            "docs: x",
            "ghp_test",
        )
        body = json.loads(requests_mock.last_request.text)
        assert body["branch"] == "feature/feat-1-init"
