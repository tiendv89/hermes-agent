"""Tests for plugins.document_repo — the document commit pipeline.

Covers:
  - StaleBaseError raised on 409/422 SHA mismatch
  - read_document: present file, 404 (new file)
  - write_document: new file (sha=None), existing file (sha provided)
  - ensure_feature_branch: already exists, absent (created), race (422)
  - ensure_pr: existing open PR returned, create when absent
  - edit_document handler: targeted edits happy path, old_string not found warning
  - write_product_spec / write_technical_design via document_repo path
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
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]


_OWNER = "testorg"
_REPO = "testws"
_TOKEN = "ghp_testtoken"
_FEATURE_ID = "my-feature"
_BASE_BRANCH = "main"
_PATH = "docs/features/my-feature/product-spec.md"
_CONTENT = "# Product Spec\n\nHello world.\n"
_ENCODED_CONTENT = base64.b64encode(_CONTENT.encode()).decode("ascii")
_SHA = "abc123def456"

_GITHUB_API = "https://api.github.com"
_BRANCH = f"feature/{_FEATURE_ID}"


# ---------------------------------------------------------------------------
# StaleBaseError
# ---------------------------------------------------------------------------


class TestStaleBaseError:
    def test_is_exception(self):
        from plugins.document_repo import StaleBaseError

        err = StaleBaseError("some/path", "sha mismatch")
        assert isinstance(err, Exception)
        assert "some/path" in str(err)
        assert "sha mismatch" in str(err)

    def test_attributes(self):
        from plugins.document_repo import StaleBaseError

        err = StaleBaseError("a/b", "detail here")
        assert err.path == "a/b"
        assert err.detail == "detail here"


# ---------------------------------------------------------------------------
# read_document
# ---------------------------------------------------------------------------


class TestReadDocument:
    def test_returns_content_and_sha(self, requests_mock):
        from plugins.document_repo import read_document

        url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/contents/{_PATH}"
        requests_mock.get(
            url,
            json={"content": _ENCODED_CONTENT + "\n", "sha": _SHA},
        )
        result = read_document(_OWNER, _REPO, _BRANCH, _PATH, _TOKEN)
        assert result["content"] == _CONTENT
        assert result["sha"] == _SHA

    def test_returns_empty_and_none_on_404(self, requests_mock):
        from plugins.document_repo import read_document

        url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/contents/{_PATH}"
        requests_mock.get(url, status_code=404)
        result = read_document(_OWNER, _REPO, _BRANCH, _PATH, _TOKEN)
        assert result["content"] == ""
        assert result["sha"] is None

    def test_passes_ref_as_query_param(self, requests_mock):
        from plugins.document_repo import read_document

        url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/contents/{_PATH}"
        requests_mock.get(
            url,
            json={"content": _ENCODED_CONTENT, "sha": _SHA},
        )
        read_document(_OWNER, _REPO, "feature/other", _PATH, _TOKEN)
        assert requests_mock.last_request.qs["ref"] == ["feature/other"]


# ---------------------------------------------------------------------------
# ensure_feature_branch
# ---------------------------------------------------------------------------


class TestEnsureFeatureBranch:
    def test_noop_when_branch_exists(self, requests_mock):
        from plugins.document_repo import ensure_feature_branch

        ref_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/git/refs/heads/{_BRANCH}"
        requests_mock.get(ref_url, json={"ref": f"refs/heads/{_BRANCH}"})
        ensure_feature_branch(_OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _TOKEN)
        # Only one GET call; no POST
        assert len(requests_mock.request_history) == 1

    def test_creates_branch_from_base_when_absent(self, requests_mock):
        from plugins.document_repo import ensure_feature_branch

        ref_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/git/refs/heads/{_BRANCH}"
        base_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/git/refs/heads/{_BASE_BRANCH}"
        create_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/git/refs"

        requests_mock.get(ref_url, status_code=404)
        requests_mock.get(base_url, json={"object": {"sha": "baseSHA"}})
        requests_mock.post(create_url, status_code=201, json={"ref": f"refs/heads/{_BRANCH}"})

        ensure_feature_branch(_OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _TOKEN)

        post_req = requests_mock.last_request
        body = json.loads(post_req.text)
        assert body["sha"] == "baseSHA"
        assert body["ref"] == f"refs/heads/{_BRANCH}"

    def test_ignores_422_on_create_race(self, requests_mock):
        from plugins.document_repo import ensure_feature_branch

        ref_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/git/refs/heads/{_BRANCH}"
        base_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/git/refs/heads/{_BASE_BRANCH}"
        create_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/git/refs"

        requests_mock.get(ref_url, status_code=404)
        requests_mock.get(base_url, json={"object": {"sha": "baseSHA"}})
        requests_mock.post(create_url, status_code=422, json={"message": "Reference already exists"})

        # Should not raise
        ensure_feature_branch(_OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _TOKEN)

    def test_rejects_invalid_feature_id(self):
        from plugins.document_repo import ensure_feature_branch

        with pytest.raises(ValueError, match="Invalid feature_id"):
            ensure_feature_branch(_OWNER, _REPO, "bad/id", _BASE_BRANCH, _TOKEN)


# ---------------------------------------------------------------------------
# write_document
# ---------------------------------------------------------------------------


class TestWriteDocument:
    def _mock_ensure_branch(self, requests_mock):
        ref_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/git/refs/heads/{_BRANCH}"
        requests_mock.get(ref_url, json={"ref": f"refs/heads/{_BRANCH}"})

    def _mock_list_prs(self, requests_mock, pulls):
        pulls_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/pulls"
        requests_mock.get(pulls_url, json=pulls)

    def _mock_contents_put(self, requests_mock, status_code=200, json_body=None):
        contents_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/contents/{_PATH}"
        requests_mock.put(
            contents_url,
            status_code=status_code,
            json=json_body or {"commit": {"sha": "commitSHA"}, "content": {}},
        )

    def test_new_file_no_sha(self, requests_mock):
        from plugins.document_repo import write_document

        self._mock_ensure_branch(requests_mock)
        self._mock_contents_put(requests_mock, status_code=201)
        self._mock_list_prs(requests_mock, [{"number": 1, "html_url": "http://pr/1", "state": "open"}])

        result = write_document(
            _OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _PATH, _CONTENT, None, "add spec", _TOKEN
        )

        assert result["commit_sha"] == "commitSHA"
        put_body = json.loads(requests_mock.request_history[-2].text)
        assert "sha" not in put_body  # No SHA for new files

    def test_existing_file_includes_sha(self, requests_mock):
        from plugins.document_repo import write_document

        self._mock_ensure_branch(requests_mock)
        self._mock_contents_put(requests_mock)
        self._mock_list_prs(requests_mock, [{"number": 1, "html_url": "http://pr/1", "state": "open"}])

        write_document(
            _OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _PATH, _CONTENT, _SHA, "update spec", _TOKEN
        )

        put_body = json.loads(requests_mock.request_history[-2].text)
        assert put_body["sha"] == _SHA

    def test_409_raises_stale_base_error(self, requests_mock):
        from plugins.document_repo import StaleBaseError, write_document

        self._mock_ensure_branch(requests_mock)
        contents_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/contents/{_PATH}"
        requests_mock.put(contents_url, status_code=409, json={"message": "Conflict"})

        with pytest.raises(StaleBaseError):
            write_document(
                _OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _PATH, _CONTENT, _SHA, "msg", _TOKEN
            )

    def test_422_raises_stale_base_error(self, requests_mock):
        from plugins.document_repo import StaleBaseError, write_document

        self._mock_ensure_branch(requests_mock)
        contents_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/contents/{_PATH}"
        requests_mock.put(
            contents_url,
            status_code=422,
            json={"message": "Validation Failed"},
        )

        with pytest.raises(StaleBaseError):
            write_document(
                _OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _PATH, _CONTENT, _SHA, "msg", _TOKEN
            )

    def test_content_is_base64_encoded(self, requests_mock):
        from plugins.document_repo import write_document

        self._mock_ensure_branch(requests_mock)
        self._mock_contents_put(requests_mock)
        self._mock_list_prs(requests_mock, [{"number": 1, "html_url": "http://pr/1", "state": "open"}])

        write_document(
            _OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _PATH, _CONTENT, None, "msg", _TOKEN
        )

        put_body = json.loads(requests_mock.request_history[-2].text)
        decoded = base64.b64decode(put_body["content"]).decode("utf-8")
        assert decoded == _CONTENT

    def test_branch_set_in_put_payload(self, requests_mock):
        from plugins.document_repo import write_document

        self._mock_ensure_branch(requests_mock)
        self._mock_contents_put(requests_mock)
        self._mock_list_prs(requests_mock, [{"number": 1, "html_url": "http://pr/1", "state": "open"}])

        write_document(
            _OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _PATH, _CONTENT, None, "msg", _TOKEN
        )

        put_body = json.loads(requests_mock.request_history[-2].text)
        assert put_body["branch"] == _BRANCH


# ---------------------------------------------------------------------------
# ensure_pr
# ---------------------------------------------------------------------------


class TestEnsurePr:
    def test_returns_existing_open_pr(self, requests_mock):
        from plugins.document_repo import ensure_pr

        pulls_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/pulls"
        requests_mock.get(
            pulls_url,
            json=[{"number": 42, "html_url": "https://github.com/org/repo/pull/42", "state": "open"}],
        )

        result = ensure_pr(_OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _TOKEN)
        assert result["number"] == 42
        assert result["url"] == "https://github.com/org/repo/pull/42"
        # Should not call POST
        assert not any(r.method == "POST" for r in requests_mock.request_history)

    def test_creates_pr_when_none_exists(self, requests_mock):
        from plugins.document_repo import ensure_pr

        pulls_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/pulls"
        requests_mock.get(pulls_url, json=[])
        requests_mock.post(
            pulls_url,
            status_code=201,
            json={"number": 7, "html_url": "https://github.com/org/repo/pull/7", "state": "open"},
        )

        result = ensure_pr(_OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _TOKEN)
        assert result["number"] == 7

    def test_pr_title_contains_feature_id(self, requests_mock):
        from plugins.document_repo import ensure_pr

        pulls_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/pulls"
        requests_mock.get(pulls_url, json=[])
        requests_mock.post(
            pulls_url,
            status_code=201,
            json={"number": 1, "html_url": "http://pr/1", "state": "open"},
        )

        ensure_pr(_OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _TOKEN)

        body = json.loads(requests_mock.last_request.text)
        assert _FEATURE_ID in body["title"]
        assert body["head"] == _BRANCH
        assert body["base"] == _BASE_BRANCH

    def test_handles_422_race_on_create(self, requests_mock):
        from plugins.document_repo import ensure_pr

        pulls_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/pulls"
        # First GET returns empty
        requests_mock.get(pulls_url, [
            {"json": []},
            {"json": [{"number": 5, "html_url": "http://pr/5", "state": "open"}]},
        ])
        # POST returns 422 (race)
        requests_mock.post(pulls_url, status_code=422, json={"message": "Validation Failed"})

        result = ensure_pr(_OWNER, _REPO, _FEATURE_ID, _BASE_BRANCH, _TOKEN)
        assert result["number"] == 5


# ---------------------------------------------------------------------------
# handle_edit_document (targeted edit tool)
# ---------------------------------------------------------------------------


_WS_CONTEXT = {
    "management_repo": "mgmt-repo",
    "repos": [{"id": "mgmt-repo", "github": f"git@github.com:{_OWNER}/{_REPO}.git"}],
}


class TestHandleEditDocument:
    def _mock_read(self, requests_mock, content=_CONTENT, sha=_SHA):
        url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/contents/{_PATH}"
        encoded = base64.b64encode(content.encode()).decode("ascii")
        requests_mock.get(url, json={"content": encoded + "\n", "sha": sha})

    def _mock_write(self, requests_mock):
        url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/contents/{_PATH}"
        requests_mock.put(url, json={"commit": {"sha": "newCommit"}, "content": {}})
        ref_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/git/refs/heads/{_BRANCH}"
        requests_mock.get(ref_url, json={"ref": f"refs/heads/{_BRANCH}"})
        pulls_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/pulls"
        requests_mock.get(
            pulls_url,
            json=[{"number": 1, "html_url": "http://pr/1", "state": "open"}],
        )

    def test_happy_path(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)

        from plugins.tools import edit as edit_tool

        self._mock_read(requests_mock)
        self._mock_write(requests_mock)

        with patch("plugins.tools.edit.get_workspace_context", return_value=_WS_CONTEXT), \
             patch("plugins.context.get_workspace_id", return_value="ws-1"), \
             patch("plugins.context.get_feature_id", return_value=_FEATURE_ID):

            result = edit_tool.handle_edit_document(
                document="product_spec",
                edits=[{"old_string": "Hello world.", "new_string": "Hello v3!"}],
                workspace_id="ws-1",
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result["conflict"] is False
        assert "pr_url" in result
        assert "commit_sha" in result

    def test_old_string_not_found_returns_warning(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)

        from plugins.tools import edit as edit_tool

        self._mock_read(requests_mock)
        self._mock_write(requests_mock)

        with patch("plugins.tools.edit.get_workspace_context", return_value=_WS_CONTEXT), \
             patch("plugins.context.get_workspace_id", return_value="ws-1"), \
             patch("plugins.context.get_feature_id", return_value=_FEATURE_ID):

            result = edit_tool.handle_edit_document(
                document="product_spec",
                edits=[{"old_string": "DOES NOT EXIST IN CONTENT", "new_string": "anything"}],
                workspace_id="ws-1",
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert "warnings" in result
        assert len(result["warnings"]) == 1

    def test_missing_github_token_returns_error(self, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)

        from plugins.tools import edit as edit_tool

        result = edit_tool.handle_edit_document(
            document="product_spec",
            edits=[{"old_string": "x", "new_string": "y"}],
            workspace_id="ws-1",
            feature_id=_FEATURE_ID,
        )
        assert result["ok"] is False
        assert "GITHUB_TOKEN" in result["error"]

    def test_unknown_document_returns_error(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)

        from plugins.tools import edit as edit_tool

        result = edit_tool.handle_edit_document(
            document="nonexistent_doc",
            edits=[{"old_string": "x", "new_string": "y"}],
            workspace_id="ws-1",
            feature_id=_FEATURE_ID,
        )
        assert result["ok"] is False
        assert "Unknown document" in result["error"]

    def test_stale_sha_returns_conflict(self, monkeypatch, requests_mock):
        monkeypatch.setenv("GITHUB_TOKEN", _TOKEN)

        from plugins.tools import edit as edit_tool

        url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/contents/{_PATH}"
        encoded = base64.b64encode(_CONTENT.encode()).decode("ascii")
        requests_mock.get(url, json={"content": encoded + "\n", "sha": _SHA})
        ref_url = f"{_GITHUB_API}/repos/{_OWNER}/{_REPO}/git/refs/heads/{_BRANCH}"
        requests_mock.get(ref_url, json={"ref": f"refs/heads/{_BRANCH}"})
        requests_mock.put(url, status_code=409, json={"message": "Conflict"})

        with patch("plugins.tools.edit.get_workspace_context", return_value=_WS_CONTEXT):
            result = edit_tool.handle_edit_document(
                document="product_spec",
                edits=[{"old_string": "Hello world.", "new_string": "Hello v3!"}],
                workspace_id="ws-1",
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert result["conflict"] is True


# ---------------------------------------------------------------------------
# apply_edits helper
# ---------------------------------------------------------------------------


class TestApplyEdits:
    def test_single_replacement(self):
        from plugins.tools.edit import _apply_edits

        content = "Hello world."
        new, warnings = _apply_edits(content, [{"old_string": "world", "new_string": "v3"}])
        assert new == "Hello v3."
        assert warnings == []

    def test_multiple_replacements_in_order(self):
        from plugins.tools.edit import _apply_edits

        content = "A B C"
        new, warnings = _apply_edits(
            content,
            [{"old_string": "A", "new_string": "1"}, {"old_string": "B", "new_string": "2"}],
        )
        assert new == "1 2 C"

    def test_missing_old_string_adds_warning(self):
        from plugins.tools.edit import _apply_edits

        content = "Hello world."
        new, warnings = _apply_edits(content, [{"old_string": "MISSING", "new_string": "x"}])
        assert new == content  # unchanged
        assert len(warnings) == 1
        assert "MISSING" in warnings[0]

    def test_first_occurrence_only_replaced(self):
        from plugins.tools.edit import _apply_edits

        content = "foo foo foo"
        new, _ = _apply_edits(content, [{"old_string": "foo", "new_string": "bar"}])
        assert new == "bar foo foo"
