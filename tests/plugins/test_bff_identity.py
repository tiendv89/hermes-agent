"""Tests for plugins/auth/bff_identity.py.

Covers:
  - sign_verify_roundtrip: signing then verifying returns the original claims
  - forged_token_rejected: tampered HMAC is rejected
  - expired_token_rejected: token with exp in the past is rejected
  - missing_separator_rejected: malformed token (no '.') is rejected
  - invalid_base64_rejected: garbage base64 is rejected
  - sign_requires_key: RuntimeError when BFF_SIGNING_KEY is absent
  - verify_requires_key: RuntimeError when BFF_SIGNING_KEY is absent
  - multi_org_roundtrip: accessible_org_ids with multiple values round-trips correctly
  - empty_accessible_orgs: empty list round-trips correctly
"""

from __future__ import annotations

import base64
import time

import pytest

from plugins.auth.bff_identity import sign_bff_identity, verify_bff_identity

_KEY = "test-signing-key-abc123"


def _make_claims(
    *,
    user_id: str = "user-uuid-1",
    org_id: str = "org-uuid-1",
    accessible_org_ids: list[str] | None = None,
    platform_role: str = "",
    exp: int | None = None,
) -> dict:
    claims: dict = {
        "user_id": user_id,
        "org_id": org_id,
        "accessible_org_ids": accessible_org_ids if accessible_org_ids is not None else ["org-uuid-1"],
        "platform_role": platform_role,
    }
    if exp is not None:
        claims["exp"] = exp
    return claims


class TestSignVerifyRoundtrip:
    def test_basic_roundtrip(self):
        claims = _make_claims()
        token = sign_bff_identity(claims, key=_KEY)
        result = verify_bff_identity(token, key=_KEY)

        assert result["user_id"] == claims["user_id"]
        assert result["org_id"] == claims["org_id"]
        assert result["accessible_org_ids"] == claims["accessible_org_ids"]
        assert result["platform_role"] == claims["platform_role"]
        assert isinstance(result["exp"], int)
        assert result["exp"] > int(time.time()) - 5

    def test_multi_org_roundtrip(self):
        orgs = ["org-uuid-1", "org-uuid-2", "org-uuid-3"]
        claims = _make_claims(accessible_org_ids=orgs)
        token = sign_bff_identity(claims, key=_KEY)
        result = verify_bff_identity(token, key=_KEY)

        assert result["accessible_org_ids"] == orgs

    def test_empty_accessible_orgs(self):
        claims = _make_claims(accessible_org_ids=[])
        token = sign_bff_identity(claims, key=_KEY)
        result = verify_bff_identity(token, key=_KEY)

        assert result["accessible_org_ids"] == []

    def test_platform_role_preserved(self):
        claims = _make_claims(platform_role="admin")
        token = sign_bff_identity(claims, key=_KEY)
        result = verify_bff_identity(token, key=_KEY)

        assert result["platform_role"] == "admin"

    def test_explicit_exp_preserved(self):
        future_exp = int(time.time()) + 30
        claims = _make_claims(exp=future_exp)
        token = sign_bff_identity(claims, key=_KEY)
        result = verify_bff_identity(token, key=_KEY)

        assert result["exp"] == future_exp

    def test_token_is_valid_base64(self):
        claims = _make_claims()
        token = sign_bff_identity(claims, key=_KEY)
        # Should not raise
        decoded = base64.b64decode(token.encode()).decode()
        assert "user_id=" in decoded
        assert ".exp=" not in decoded  # exp is part of claims, not separate
        assert ",exp=" in decoded


class TestForgedTokenRejected:
    def test_tampered_hmac_rejected(self):
        claims = _make_claims()
        token = sign_bff_identity(claims, key=_KEY)

        decoded = base64.b64decode(token.encode()).decode()
        # Flip the last character of the HMAC hex
        tampered = decoded[:-1] + ("0" if decoded[-1] != "0" else "1")
        forged_token = base64.b64encode(tampered.encode()).decode()

        with pytest.raises(ValueError, match="signature verification failed"):
            verify_bff_identity(forged_token, key=_KEY)

    def test_different_key_rejected(self):
        claims = _make_claims()
        token = sign_bff_identity(claims, key=_KEY)

        with pytest.raises(ValueError, match="signature verification failed"):
            verify_bff_identity(token, key="wrong-key")

    def test_tampered_user_id_rejected(self):
        claims = _make_claims()
        token = sign_bff_identity(claims, key=_KEY)

        decoded = base64.b64decode(token.encode()).decode()
        # Alter user_id in the claims portion
        tampered = decoded.replace("user_id=user-uuid-1", "user_id=attacker-uuid")
        forged_token = base64.b64encode(tampered.encode()).decode()

        with pytest.raises(ValueError):
            verify_bff_identity(forged_token, key=_KEY)


class TestExpiredTokenRejected:
    def test_expired_token_raises(self):
        past_exp = int(time.time()) - 10
        claims = _make_claims(exp=past_exp)
        token = sign_bff_identity(claims, key=_KEY)

        with pytest.raises(ValueError, match="expired"):
            verify_bff_identity(token, key=_KEY)


class TestMalformedTokenRejected:
    def test_missing_dot_separator(self):
        # Token with no '.' between claims and HMAC
        bad = base64.b64encode(b"user_id=x").decode()
        with pytest.raises(ValueError, match="separator"):
            verify_bff_identity(bad, key=_KEY)

    def test_invalid_base64(self):
        with pytest.raises(ValueError, match="base64"):
            verify_bff_identity("not valid base64!!!", key=_KEY)


class TestKeyRequirement:
    def test_sign_requires_key(self, monkeypatch):
        monkeypatch.delenv("BFF_SIGNING_KEY", raising=False)
        with pytest.raises(RuntimeError, match="BFF_SIGNING_KEY"):
            sign_bff_identity(_make_claims(), key=None)

    def test_verify_requires_key(self, monkeypatch):
        monkeypatch.delenv("BFF_SIGNING_KEY", raising=False)
        with pytest.raises(RuntimeError, match="BFF_SIGNING_KEY"):
            verify_bff_identity("some-token", key=None)

    def test_sign_uses_env_key(self, monkeypatch):
        monkeypatch.setenv("BFF_SIGNING_KEY", _KEY)
        claims = _make_claims()
        token = sign_bff_identity(claims, key=None)
        result = verify_bff_identity(token, key=_KEY)
        assert result["user_id"] == claims["user_id"]

    def test_verify_uses_env_key(self, monkeypatch):
        monkeypatch.setenv("BFF_SIGNING_KEY", _KEY)
        claims = _make_claims()
        token = sign_bff_identity(claims, key=_KEY)
        result = verify_bff_identity(token, key=None)
        assert result["user_id"] == claims["user_id"]
