"""Tests for plugins/storage_service_client.py's write_document_content.

Covers the create-then-retry behavior: storage-service's content PUT is
edit-only (404s "document not found" for any path without an existing
document row) — write_document_content must transparently create the row
via POST /api/documents and retry the PUT once, for both feature-scoped and
workspace-root (feature_id="") paths.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from plugins import storage_service_client as ssc  # noqa: E402

_BASE_URL = "http://storage-service.test"
_TOKEN = "test-token"
_WORKSPACE_ID = "ws-1"
_FEATURE_ID = "feat-1"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("STORAGE_SERVICE_URL", _BASE_URL)
    monkeypatch.setenv("STORAGE_SERVICE_TOKEN", _TOKEN)


def _resp(status_code, json_body=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.ok = 200 <= status_code < 300
    resp.json.return_value = json_body or {}
    return resp


class TestWriteDocumentContentCreateOnMissing:
    def test_404_creates_document_then_retries_put(self):
        """First PUT 404s -> POST /api/documents -> PUT succeeds."""
        not_found = _resp(404, {"error": "document not found"})
        created = _resp(201, {"id": "doc-1"})
        ok = _resp(200, {"version_id": "v1"})

        with (
            patch("requests.put", side_effect=[not_found, ok]) as mock_put,
            patch("requests.post", return_value=created) as mock_post,
        ):
            result = ssc.write_document_content(
                _WORKSPACE_ID, _FEATURE_ID, "api.txt", "hello", user_id="u1", org_id="org-1"
            )

        assert result == {"ok": True, "version_id": "v1"}
        assert mock_put.call_count == 2
        mock_post.assert_called_once()
        post_url, post_kwargs = mock_post.call_args
        assert post_url[0] == f"{_BASE_URL}/api/documents"
        assert post_kwargs["json"] == {
            "workspace_id": _WORKSPACE_ID,
            "feature_id": _FEATURE_ID,
            "path": "api.txt",
        }

    def test_404_creates_workspace_root_document_when_no_feature_id(self):
        """feature_id="" still creates the row (workspace-root) and retries."""
        not_found = _resp(404, {"error": "document not found"})
        ok = _resp(200, {"version_id": "v2"})

        with (
            patch("requests.put", side_effect=[not_found, ok]),
            patch("requests.post", return_value=_resp(201)) as mock_post,
        ):
            result = ssc.write_document_content(
                _WORKSPACE_ID, "", "api.txt", "hello", user_id="u1", org_id="org-1"
            )

        assert result == {"ok": True, "version_id": "v2"}
        _, post_kwargs = mock_post.call_args
        assert post_kwargs["json"]["feature_id"] == ""

    def test_no_retry_when_put_succeeds_first_try(self):
        """A pre-existing document's PUT succeeds without ever calling create."""
        ok = _resp(200, {"version_id": "v3"})

        with (
            patch("requests.put", return_value=ok) as mock_put,
            patch("requests.post") as mock_post,
        ):
            result = ssc.write_document_content(
                _WORKSPACE_ID, _FEATURE_ID, "product_spec.md", "hello", user_id="u1", org_id="org-1"
            )

        assert result == {"ok": True, "version_id": "v3"}
        assert mock_put.call_count == 1
        mock_post.assert_not_called()

    def test_create_failure_raises_storage_service_error(self):
        """If the create POST itself fails, raise — do not retry the PUT."""
        not_found = _resp(404, {"error": "document not found"})
        create_error = _resp(500, {"error": "boom"})

        with (
            patch("requests.put", return_value=not_found) as mock_put,
            patch("requests.post", return_value=create_error),
        ):
            with pytest.raises(ssc.StorageServiceError):
                ssc.write_document_content(
                    _WORKSPACE_ID, _FEATURE_ID, "api.txt", "hello", user_id="u1", org_id="org-1"
                )

        assert mock_put.call_count == 1

    def test_retry_still_failing_raises_storage_service_error(self):
        """If the retried PUT still fails after create, surface that error."""
        not_found = _resp(404, {"error": "document not found"})
        still_bad = _resp(500, {"error": "boom"})

        with (
            patch("requests.put", side_effect=[not_found, still_bad]),
            patch("requests.post", return_value=_resp(201)),
        ):
            with pytest.raises(ssc.StorageServiceError):
                ssc.write_document_content(
                    _WORKSPACE_ID, _FEATURE_ID, "api.txt", "hello", user_id="u1", org_id="org-1"
                )

    def test_non_404_error_never_calls_create(self):
        """A non-404 PUT failure (e.g. 403) must not trigger document creation."""
        forbidden = _resp(403, {"error": "forbidden"})

        with (
            patch("requests.put", return_value=forbidden),
            patch("requests.post") as mock_post,
        ):
            with pytest.raises(ssc.StorageServiceError):
                ssc.write_document_content(
                    _WORKSPACE_ID, _FEATURE_ID, "api.txt", "hello", user_id="u1", org_id="org-1"
                )

        mock_post.assert_not_called()
