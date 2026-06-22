"""Tests for feature-initialization-compatible T3 changes.

Covers:
  - _resolve_document_branch: init PR open, init PR merged, no init PR
  - _owner_guard_ts_only: ts feature (None) and go feature (skip dict)
  - _write_artifact: init PR branch path calls commit_to_branch + request_approval
  - _write_artifact: feature branch path calls write_document + request_approval
  - db.get_feature_detail: returns owner and init_pr_url
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
# _owner_guard_ts_only
# ---------------------------------------------------------------------------


class TestOwnerGuardTsOnly:
    def test_returns_none_for_ts(self):
        from plugins.tools.artifacts import _owner_guard_ts_only

        assert _owner_guard_ts_only("ts") is None

    def test_returns_none_when_absent(self):
        from plugins.tools.artifacts import _owner_guard_ts_only

        assert _owner_guard_ts_only(None) is None

    def test_returns_skip_dict_for_go(self):
        from plugins.tools.artifacts import _owner_guard_ts_only

        result = _owner_guard_ts_only("go")
        assert result is not None
        assert result["ok"] is False
        assert result["skipped"] is True
        assert (
            "go" in result["reason"].lower() or "postgres" in result["reason"].lower()
        )

    def test_go_skip_message_mentions_database(self):
        from plugins.tools.artifacts import _owner_guard_ts_only

        result = _owner_guard_ts_only("go")
        assert result is not None
        assert (
            "database" in result["reason"].lower() or "db" in result["reason"].lower()
        )


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
# _write_artifact — init PR branch path
# ---------------------------------------------------------------------------


class TestWriteArtifactInitPrPath:
    def test_commits_to_init_branch_when_pr_open(self, monkeypatch):
        """When init PR is open, commit_to_branch is called (not write_document)."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        with (
            patch("plugins.tools.artifacts.get_workspace_context", return_value={}),
            patch(
                "plugins.tools.artifacts._resolve_management_repo",
                return_value=("org", "repo"),
            ),
            patch(
                "plugins.tools.artifacts.get_feature_detail",
                return_value={
                    "init_pr_url": "https://github.com/org/repo/pull/7",
                    "owner": "ts",
                },
            ),
            patch(
                "plugins.tools.artifacts._resolve_document_branch",
                return_value=(
                    "feature/feat-1-init",
                    "https://github.com/org/repo/pull/7",
                ),
            ),
            patch(
                "plugins.tools.artifacts.read_document",
                return_value={"content": "# old", "sha": "abc"},
            ),
            patch(
                "plugins.tools.artifacts.commit_to_branch",
                return_value="newsha123",
            ) as mock_commit,
            patch("plugins.tools.artifacts.write_document") as mock_write,
            patch("plugins.tools.approval.handle", return_value={"ok": False}),
        ):
            from plugins.tools.artifacts import _write_artifact

            result = _write_artifact(
                "ws-1",
                "feat-1",
                "product-spec.md",
                "# Spec",
                "docs: update",
                "product_spec",
            )
        assert result["ok"] is True
        assert result["commit_sha"] == "newsha123"
        assert result["pr_url"] == "https://github.com/org/repo/pull/7"
        mock_commit.assert_called_once()
        mock_write.assert_not_called()

    def test_calls_write_document_for_feature_branch(self, monkeypatch):
        """When feature branch path, write_document is called (not commit_to_branch)."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        with (
            patch("plugins.tools.artifacts.get_workspace_context", return_value={}),
            patch(
                "plugins.tools.artifacts._resolve_management_repo",
                return_value=("org", "repo"),
            ),
            patch(
                "plugins.tools.artifacts.get_feature_detail",
                return_value={"init_pr_url": None, "owner": "ts"},
            ),
            patch(
                "plugins.tools.artifacts._resolve_document_branch",
                return_value=("feature/feat-1", None),
            ),
            patch(
                "plugins.tools.artifacts.read_document",
                return_value={"content": "", "sha": None},
            ),
            patch(
                "plugins.tools.artifacts.write_document",
                return_value={
                    "commit_sha": "sha999",
                    "pr": {"url": "https://github.com/org/repo/pull/1"},
                },
            ) as mock_write,
            patch("plugins.tools.artifacts.commit_to_branch") as mock_commit,
            patch("plugins.tools.approval.handle", return_value={"ok": False}),
        ):
            from plugins.tools.artifacts import _write_artifact

            result = _write_artifact(
                "ws-1",
                "feat-1",
                "product-spec.md",
                "# Spec",
                "docs: update",
                "product_spec",
            )
        assert result["ok"] is True
        assert result["commit_sha"] == "sha999"
        assert result["pr_url"] == "https://github.com/org/repo/pull/1"
        mock_write.assert_called_once()
        mock_commit.assert_not_called()

    def test_approval_request_included_in_response(self, monkeypatch):
        """Approval request dict is included in the response when request_approval succeeds."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        fake_approval = {
            "feature_id": "feat-1",
            "stage": "product_spec",
            "review_status": "draft",
        }
        with (
            patch("plugins.tools.artifacts.get_workspace_context", return_value={}),
            patch(
                "plugins.tools.artifacts._resolve_management_repo",
                return_value=("o", "r"),
            ),
            patch(
                "plugins.tools.artifacts.get_feature_detail",
                return_value={"init_pr_url": None, "owner": "ts"},
            ),
            patch(
                "plugins.tools.artifacts._resolve_document_branch",
                return_value=("feature/feat-1", None),
            ),
            patch(
                "plugins.tools.artifacts.read_document",
                return_value={"content": "", "sha": None},
            ),
            patch(
                "plugins.tools.artifacts.write_document",
                return_value={"commit_sha": "abc", "pr": {"url": "http://pr"}},
            ),
            patch(
                "plugins.tools.approval.handle",
                return_value={"ok": True, "approval_request": fake_approval},
            ),
        ):
            from plugins.tools.artifacts import _write_artifact

            result = _write_artifact(
                "ws-1", "feat-1", "product-spec.md", "# x", "docs: x", "product_spec"
            )
        assert result["ok"] is True
        assert "approval_request" in result
        assert result["approval_request"]["stage"] == "product_spec"

    def test_approval_failure_does_not_fail_write(self, monkeypatch):
        """A request_approval failure should not cause the write to fail."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        with (
            patch("plugins.tools.artifacts.get_workspace_context", return_value={}),
            patch(
                "plugins.tools.artifacts._resolve_management_repo",
                return_value=("o", "r"),
            ),
            patch(
                "plugins.tools.artifacts.get_feature_detail",
                return_value={"init_pr_url": None, "owner": "ts"},
            ),
            patch(
                "plugins.tools.artifacts._resolve_document_branch",
                return_value=("feature/feat-1", None),
            ),
            patch(
                "plugins.tools.artifacts.read_document",
                return_value={"content": "", "sha": None},
            ),
            patch(
                "plugins.tools.artifacts.write_document",
                return_value={"commit_sha": "abc", "pr": {"url": "http://pr"}},
            ),
            patch(
                "plugins.tools.approval.handle",
                side_effect=RuntimeError("approval down"),
            ),
        ):
            from plugins.tools.artifacts import _write_artifact

            result = _write_artifact(
                "ws-1", "feat-1", "product-spec.md", "# x", "docs: x", "product_spec"
            )
        assert result["ok"] is True
        assert "approval_request" not in result

    def test_db_unavailable_falls_back_to_no_init_pr(self, monkeypatch):
        """When DB lookup fails, init_pr_url defaults to None (pre-existing feature path)."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        with (
            patch("plugins.tools.artifacts.get_workspace_context", return_value={}),
            patch(
                "plugins.tools.artifacts._resolve_management_repo",
                return_value=("o", "r"),
            ),
            patch(
                "plugins.tools.artifacts.get_feature_detail",
                side_effect=RuntimeError("db down"),
            ),
            patch(
                "plugins.tools.artifacts._resolve_document_branch",
                return_value=("feature/feat-1", None),
            ) as mock_resolve,
            patch(
                "plugins.tools.artifacts.read_document",
                return_value={"content": "", "sha": None},
            ),
            patch(
                "plugins.tools.artifacts.write_document",
                return_value={"commit_sha": "abc", "pr": {"url": "http://pr"}},
            ),
            patch("plugins.tools.approval.handle", return_value={"ok": False}),
        ):
            from plugins.tools.artifacts import _write_artifact

            result = _write_artifact(
                "ws-1", "feat-1", "product-spec.md", "# x", "docs: x", "product_spec"
            )
        assert result["ok"] is True
        # init_pr_url passed as None when DB lookup fails
        call_args = mock_resolve.call_args
        assert call_args[0][3] is None  # init_pr_url positional arg


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


# ---------------------------------------------------------------------------
# handle_write_product_spec — integration with new branch logic
# ---------------------------------------------------------------------------


class TestHandleWriteProductSpecInitPr:
    def test_writes_to_init_branch_when_pr_open(self, monkeypatch, requests_mock):
        """write_product_spec routes to init PR branch when init_pr_url is set + branch exists."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")

        workspace_ctx = {
            "management_repo": "mgmt",
            "repos": [{"id": "mgmt", "github": "git@github.com:org/ws.git"}],
        }
        feature_detail = {
            "init_pr_url": "https://github.com/org/ws/pull/5",
            "owner": "ts",
        }

        # init branch exists
        requests_mock.get(
            "https://api.github.com/repos/org/ws/git/refs/heads/feature/feat-1-init",
            json={"ref": "refs/heads/feature/feat-1-init"},
        )
        # read existing SHA on init branch → 404 (new file)
        requests_mock.get(
            "https://api.github.com/repos/org/ws/contents/docs/features/feat-1/product-spec.md",
            status_code=404,
        )
        # commit to init branch
        requests_mock.put(
            "https://api.github.com/repos/org/ws/contents/docs/features/feat-1/product-spec.md",
            json={"commit": {"sha": "initsha"}},
            status_code=201,
        )

        with (
            patch(
                "plugins.tools.artifacts.get_workspace_context",
                return_value=workspace_ctx,
            ),
            patch(
                "plugins.tools.artifacts.get_feature_detail",
                return_value=feature_detail,
            ),
            patch("plugins.tools.approval.handle", return_value={"ok": False}),
        ):
            from plugins.tools.artifacts import handle_write_product_spec

            result = handle_write_product_spec(
                content="# Spec", workspace_id="ws-1", feature_id="feat-1"
            )
        assert result["ok"] is True
        assert result["commit_sha"] == "initsha"
        assert result["pr_url"] == "https://github.com/org/ws/pull/5"
        # Verify the PUT went to the init branch
        put_body = json.loads(requests_mock.last_request.text)
        assert put_body["branch"] == "feature/feat-1-init"

    def test_writes_to_feature_branch_when_no_init_pr(self, monkeypatch, requests_mock):
        """write_product_spec falls back to feature/<id> when init_pr_url is None."""
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")

        workspace_ctx = {
            "management_repo": "mgmt",
            "repos": [{"id": "mgmt", "github": "git@github.com:org/ws.git"}],
        }
        feature_detail = {"init_pr_url": None, "owner": "ts"}

        # ensure_feature_branch check
        requests_mock.get(
            "https://api.github.com/repos/org/ws/git/refs/heads/feature/feat-1",
            json={"ref": "refs/heads/feature/feat-1"},
        )
        # read existing SHA on feature branch → 404 (new file)
        requests_mock.get(
            "https://api.github.com/repos/org/ws/contents/docs/features/feat-1/product-spec.md",
            status_code=404,
        )
        # commit to feature branch
        requests_mock.put(
            "https://api.github.com/repos/org/ws/contents/docs/features/feat-1/product-spec.md",
            json={"commit": {"sha": "featsha"}},
            status_code=201,
        )
        # ensure_pr
        requests_mock.get(
            "https://api.github.com/repos/org/ws/pulls",
            json=[
                {
                    "number": 1,
                    "html_url": "https://github.com/org/ws/pull/1",
                    "state": "open",
                }
            ],
        )

        with (
            patch(
                "plugins.tools.artifacts.get_workspace_context",
                return_value=workspace_ctx,
            ),
            patch(
                "plugins.tools.artifacts.get_feature_detail",
                return_value=feature_detail,
            ),
            patch("plugins.tools.approval.handle", return_value={"ok": False}),
        ):
            from plugins.tools.artifacts import handle_write_product_spec

            result = handle_write_product_spec(
                content="# Spec", workspace_id="ws-1", feature_id="feat-1"
            )
        assert result["ok"] is True
        assert result["commit_sha"] == "featsha"
        # pr_url comes from ensure_pr, which found an existing open PR
        assert result["pr_url"] == "https://github.com/org/ws/pull/1"
