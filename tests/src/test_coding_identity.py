"""Unit tests for coding_identity.py — trusted-header identity for IDE requests.

The IDE extensions' device-flow JWT is verified by workflow-bff, not this
service (see src/api/coding_identity.py's module docstring) — these tests
mirror test_admin_models.py's MagicMock(spec=Request) pattern for
require_identity/require_platform_admin, since require_coding_identity is
now built directly on require_identity.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _mock_request(headers: dict) -> Request:
    mock_request = MagicMock(spec=Request)
    mock_request.headers = headers
    return mock_request


class TestRequireCodingIdentity:
    """Direct unit tests for the require_coding_identity dependency."""

    def test_trusted_headers_return_identity(self, monkeypatch):
        """BFF-injected headers populate a CodingIdentity, no JWT involved."""
        monkeypatch.delenv("GATEWAY_SERVICE_TOKEN", raising=False)
        from src.api.coding_identity import require_coding_identity

        request = _mock_request(
            {
                "X-User-Id": "user-abc",
                "X-Org-Id": "org-xyz",
                "X-Accessible-Org-Ids": "org-xyz,org-2",
            }
        )

        identity = require_coding_identity(request)
        assert identity.user_id == "user-abc"
        assert identity.org_id == "org-xyz"
        assert identity.accessible_workspace_ids == ["org-xyz", "org-2"]

    def test_missing_accessible_org_ids_defaults_empty(self, monkeypatch):
        monkeypatch.delenv("GATEWAY_SERVICE_TOKEN", raising=False)
        from src.api.coding_identity import require_coding_identity

        request = _mock_request({"X-User-Id": "user-abc", "X-Org-Id": "org-xyz"})

        identity = require_coding_identity(request)
        assert identity.accessible_workspace_ids == []

    def test_missing_headers_default_to_empty_strings(self, monkeypatch):
        """No X-User-Id/X-Org-Id at all — fields default empty, no crash."""
        monkeypatch.delenv("GATEWAY_SERVICE_TOKEN", raising=False)
        from src.api.coding_identity import require_coding_identity

        identity = require_coding_identity(_mock_request({}))
        assert identity.user_id == ""
        assert identity.org_id == ""
        assert identity.accessible_workspace_ids == []

    def test_service_token_required_when_configured(self, monkeypatch):
        """When GATEWAY_SERVICE_TOKEN is set, a missing/wrong Authorization 401s."""
        monkeypatch.setenv("GATEWAY_SERVICE_TOKEN", "expected-token")
        from src.api.coding_identity import require_coding_identity

        request = _mock_request({"X-User-Id": "user-abc"})
        with pytest.raises(HTTPException) as exc_info:
            require_coding_identity(request)
        assert exc_info.value.status_code == 401

    def test_service_token_valid_passes(self, monkeypatch):
        monkeypatch.setenv("GATEWAY_SERVICE_TOKEN", "expected-token")
        from src.api.coding_identity import require_coding_identity

        request = _mock_request(
            {
                "Authorization": "Bearer expected-token",
                "X-User-Id": "user-abc",
                "X-Org-Id": "org-xyz",
            }
        )
        identity = require_coding_identity(request)
        assert identity.user_id == "user-abc"


class TestCodingIdentityDataclass:
    def test_defaults(self):
        from src.api.coding_identity import CodingIdentity

        ci = CodingIdentity()
        assert ci.user_id == ""
        assert ci.org_id == ""
        assert ci.accessible_workspace_ids == []

    def test_full_population(self):
        from src.api.coding_identity import CodingIdentity

        ci = CodingIdentity(
            user_id="u1", org_id="o1", accessible_workspace_ids=["ws-a"]
        )
        assert ci.user_id == "u1"
        assert ci.org_id == "o1"
        assert ci.accessible_workspace_ids == ["ws-a"]
