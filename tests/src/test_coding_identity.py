"""Unit tests for coding_identity.py — JWT authentication for IDE extensions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import HTTPException

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# JWT helper — build HS256 tokens for testing
# ---------------------------------------------------------------------------


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _sign(payload: dict, secret: str) -> str:
    header = _b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload_b64 = _b64url(json.dumps(payload).encode())
    signing_input = f"{header}.{payload_b64}"
    sig = _b64url(
        hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    )
    return f"{header}.{payload_b64}.{sig}"


# ---------------------------------------------------------------------------
# coding_identity.require_coding_identity
# ---------------------------------------------------------------------------


def _call_identity(
    secret: str | None, token: str
) -> "CodingIdentity":  # noqa: F821
    """Call require_coding_identity with a mock Authorization header."""
    from src.api.coding_identity import require_coding_identity

    if secret is not None:
        with patch.dict(os.environ, {"CODING_JWT_SECRET": secret}):
            return require_coding_identity(authorization=f"Bearer {token}")
    else:
        with patch.dict(os.environ, {}, clear=True):
            return require_coding_identity(authorization=f"Bearer {token}")


class TestRequireCodingIdentity:
    """Direct unit tests for the require_coding_identity dependency."""

    def test_valid_jwt_returns_identity(self):
        """A valid HS256-signed JWT returns a populated CodingIdentity."""
        secret = "test-secret-123"
        payload = {
            "sub": "user-abc",
            "org": "org-xyz",
            "workspaces": ["ws-1", "ws-2"],
            "exp": int(time.time()) + 3600,
        }
        token = _sign(payload, secret)

        identity = _call_identity(secret, token)
        assert identity.user_id == "user-abc"
        assert identity.org_id == "org-xyz"
        assert identity.accessible_workspace_ids == ["ws-1", "ws-2"]

    def test_non_bearer_token_raises_401(self):
        """A token not prefixed with 'Bearer ' raises 401."""
        from src.api.coding_identity import require_coding_identity

        with patch.dict(os.environ, {"CODING_JWT_SECRET": "secret"}):
            with pytest.raises(HTTPException) as exc_info:
                require_coding_identity(authorization="Basic dGVzdA==")
            assert exc_info.value.status_code == 401
            assert "Bearer" in exc_info.value.detail

    def test_invalid_signature_raises_401(self):
        """A JWT signed with the wrong secret raises 401."""
        from src.api.coding_identity import require_coding_identity

        good_secret = "good-secret"
        bad_secret = "bad-secret"
        payload = {
            "sub": "user-abc",
            "org": "org-xyz",
            "exp": int(time.time()) + 3600,
        }
        token = _sign(payload, bad_secret)

        with patch.dict(os.environ, {"CODING_JWT_SECRET": good_secret}):
            with pytest.raises(HTTPException) as exc_info:
                require_coding_identity(authorization=f"Bearer {token}")
            assert exc_info.value.status_code == 401
            assert "signature" in exc_info.value.detail.lower()

    def test_expired_token_raises_401(self):
        """An expired JWT raises 401."""
        from src.api.coding_identity import require_coding_identity

        secret = "test-secret"
        payload = {
            "sub": "user-abc",
            "org": "org-xyz",
            "exp": int(time.time()) - 3600,  # 1 hour ago
        }
        token = _sign(payload, secret)

        with patch.dict(os.environ, {"CODING_JWT_SECRET": secret}):
            with pytest.raises(HTTPException) as exc_info:
                require_coding_identity(authorization=f"Bearer {token}")
            assert exc_info.value.status_code == 401
            assert "expired" in exc_info.value.detail.lower()

    def test_malformed_token_format_raises_401(self):
        """A JWT with invalid base64 in payload raises 401."""
        from src.api.coding_identity import require_coding_identity

        with patch.dict(os.environ, {"CODING_JWT_SECRET": "secret"}):
            with pytest.raises(HTTPException) as exc_info:
                # Valid header + garbage payload + garbage sig
                header = _b64url(json.dumps({"alg": "HS256"}).encode())
                require_coding_identity(
                    authorization=f"Bearer {header}.!!!NOT-BASE64!!!.sig"
                )
            assert exc_info.value.status_code == 401

    def test_no_secret_configured_trusts_token(self):
        """When CODING_JWT_SECRET is unset, the payload is trusted as-is."""
        payload = {
            "sub": "dev-user",
            "org": "dev-org",
            "workspaces": [],
        }
        header = _b64url(json.dumps({"alg": "none"}).encode())
        payload_b64 = _b64url(json.dumps(payload).encode())
        token = f"{header}.{payload_b64}.any-signature"

        identity = _call_identity(None, token)
        assert identity.user_id == "dev-user"
        assert identity.org_id == "dev-org"
        assert identity.accessible_workspace_ids == []

    def test_empty_fields_when_no_sub(self):
        """When the JWT payload has no sub/org, fields default to empty."""
        payload = {"exp": int(time.time()) + 3600}
        token = _sign(payload, "secret")

        identity = _call_identity("secret", token)
        assert identity.user_id == ""
        assert identity.org_id == ""


# ---------------------------------------------------------------------------
# CodingIdentity dataclass
# ---------------------------------------------------------------------------


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
