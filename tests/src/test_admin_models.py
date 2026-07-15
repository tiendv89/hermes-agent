"""Tests for m4-admin-control-model-config T4.

Covers:
  - platform_role_client.has_role: allowed, denied, fail-closed paths
  - require_platform_admin: allowed, denied, fail-closed (403 on user-service unreachable)
  - admin route CRUD: list, create, patch
  - model_catalog.py DB-backed is_supported / default_model / resolve_model
  - one-default invariant: setting new default clears old one
  - retiring the current default is rejected with 400
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers / stubs for store
# ---------------------------------------------------------------------------


class _FakeCatalogRow:
    """Simulates a ModelCatalog ORM row for unit tests."""

    def __init__(
        self,
        model_id: str,
        display_name: str,
        provider: str,
        is_active: bool = True,
        is_default: bool = False,
    ) -> None:
        self.model_id = model_id
        self.display_name = display_name
        self.provider = provider
        self.is_active = is_active
        self.is_default = is_default
        self.created_at = None
        self.updated_at = None


_CATALOG = {
    "claude-sonnet-4-6": _FakeCatalogRow("claude-sonnet-4-6", "Claude Sonnet 4.6", "anthropic", True, True),
    "claude-opus-4-8": _FakeCatalogRow("claude-opus-4-8", "Claude Opus 4.8", "anthropic", True, False),
    "deepseek-v4-flash": _FakeCatalogRow("deepseek-v4-flash", "DeepSeek V4 Flash", "deepseek", True, False),
}

_INACTIVE = _FakeCatalogRow("old-model", "Old Model", "anthropic", False, False)


def _fake_db():
    return MagicMock()


# ---------------------------------------------------------------------------
# platform_role_client tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_has_role_returns_false_when_url_not_set(monkeypatch):
    """has_role fails closed when USER_SERVICE_URL is not set."""
    monkeypatch.delenv("USER_SERVICE_URL", raising=False)
    from src.services import platform_role_client

    result = await platform_role_client.has_role("user-1", "platform_admin")
    assert result is False


@pytest.mark.asyncio
async def test_has_role_returns_true_on_200_with_has_role_true(monkeypatch):
    """has_role returns True when user-service returns has_role=true."""
    monkeypatch.setenv("USER_SERVICE_URL", "http://user-service:8082")

    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.json = AsyncMock(return_value={"has_role": True})
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=False)

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.get = MagicMock(return_value=fake_resp)

    from src.services import platform_role_client
    with patch("src.services.platform_role_client.aiohttp.ClientSession", return_value=fake_session):
        result = await platform_role_client.has_role("admin-user", "platform_admin")

    assert result is True


@pytest.mark.asyncio
async def test_has_role_returns_false_on_200_with_has_role_false(monkeypatch):
    """has_role returns False when user-service returns has_role=false."""
    monkeypatch.setenv("USER_SERVICE_URL", "http://user-service:8082")

    fake_resp = MagicMock()
    fake_resp.status = 200
    fake_resp.json = AsyncMock(return_value={"has_role": False})
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=False)

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.get = MagicMock(return_value=fake_resp)

    from src.services import platform_role_client
    with patch("src.services.platform_role_client.aiohttp.ClientSession", return_value=fake_session):
        result = await platform_role_client.has_role("regular-user", "platform_admin")

    assert result is False


@pytest.mark.asyncio
async def test_has_role_fails_closed_on_non_200(monkeypatch):
    """has_role returns False (fail-closed) when user-service responds with non-200."""
    monkeypatch.setenv("USER_SERVICE_URL", "http://user-service:8082")

    fake_resp = MagicMock()
    fake_resp.status = 503
    fake_resp.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_resp.__aexit__ = AsyncMock(return_value=False)

    fake_session = MagicMock()
    fake_session.__aenter__ = AsyncMock(return_value=fake_session)
    fake_session.__aexit__ = AsyncMock(return_value=False)
    fake_session.get = MagicMock(return_value=fake_resp)

    from src.services import platform_role_client
    with patch("src.services.platform_role_client.aiohttp.ClientSession", return_value=fake_session):
        result = await platform_role_client.has_role("admin-user", "platform_admin")

    assert result is False


@pytest.mark.asyncio
async def test_has_role_fails_closed_on_network_error(monkeypatch):
    """has_role returns False (fail-closed) when user-service is unreachable.

    This is the explicit fail-closed test required by the task spec — a network
    error on the role check must return False (deny), not True (allow). This
    differs from cost_client.py which fails open for availability.
    """
    monkeypatch.setenv("USER_SERVICE_URL", "http://user-service:8082")

    from src.services import platform_role_client
    with patch(
        "src.services.platform_role_client.aiohttp.ClientSession",
        side_effect=OSError("connection refused"),
    ):
        result = await platform_role_client.has_role("admin-user", "platform_admin")

    # Must be False — fail-closed, never True on error.
    assert result is False


# ---------------------------------------------------------------------------
# require_platform_admin tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_require_platform_admin_allows_admin(monkeypatch):
    """require_platform_admin returns identity when user has platform_admin role."""
    from fastapi import Request
    from src.api.identity import require_platform_admin

    mock_request = MagicMock(spec=Request)
    mock_request.headers = {
        "Authorization": "Bearer test-token",
        "X-User-Id": "admin-user-123",
        "X-Org-Id": "org-1",
    }
    monkeypatch.delenv("GATEWAY_SERVICE_TOKEN", raising=False)

    with patch("src.services.platform_role_client.has_role", new=AsyncMock(return_value=True)):
        identity = await require_platform_admin(mock_request)

    assert identity.user_id == "admin-user-123"


@pytest.mark.asyncio
async def test_require_platform_admin_denies_non_admin(monkeypatch):
    """require_platform_admin raises 403 when user does not have platform_admin role."""
    from fastapi import Request
    from src.api.identity import require_platform_admin

    mock_request = MagicMock(spec=Request)
    mock_request.headers = {
        "Authorization": "Bearer test-token",
        "X-User-Id": "regular-user-456",
        "X-Org-Id": "org-1",
    }
    monkeypatch.delenv("GATEWAY_SERVICE_TOKEN", raising=False)

    with patch("src.services.platform_role_client.has_role", new=AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc_info:
            await require_platform_admin(mock_request)

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_require_platform_admin_fails_closed_on_unreachable_user_service(monkeypatch):
    """require_platform_admin raises 403 when user-service is unreachable (fail-closed).

    This is the explicit fail-closed test — unlike cost_client.py's fail-open, an
    authorization check must deny when user-service cannot be reached.
    """
    from fastapi import Request
    from src.api.identity import require_platform_admin

    mock_request = MagicMock(spec=Request)
    mock_request.headers = {
        "Authorization": "Bearer test-token",
        "X-User-Id": "admin-user-123",
        "X-Org-Id": "org-1",
    }
    monkeypatch.delenv("GATEWAY_SERVICE_TOKEN", raising=False)

    # has_role returns False on network errors (fail-closed in platform_role_client)
    with patch("src.services.platform_role_client.has_role", new=AsyncMock(return_value=False)):
        with pytest.raises(HTTPException) as exc_info:
            await require_platform_admin(mock_request)

    # Must be 403 — not 200, not 500
    assert exc_info.value.status_code == 403


# ---------------------------------------------------------------------------
# model_catalog.py DB-backed function tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_supported_returns_true_for_active_model():
    from src.api.model_catalog import is_supported

    db = _fake_db()
    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=_CATALOG["claude-sonnet-4-6"])):
        result = await is_supported(db, "claude-sonnet-4-6")

    assert result is True


@pytest.mark.asyncio
async def test_is_supported_returns_false_for_inactive_model():
    from src.api.model_catalog import is_supported

    db = _fake_db()
    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=_INACTIVE)):
        result = await is_supported(db, "old-model")

    assert result is False


@pytest.mark.asyncio
async def test_is_supported_returns_false_for_unknown_model():
    from src.api.model_catalog import is_supported

    db = _fake_db()
    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=None)):
        result = await is_supported(db, "unknown-model")

    assert result is False


@pytest.mark.asyncio
async def test_resolve_model_anthropic(monkeypatch):
    """resolve_model returns correct anthropic credentials for an active model."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    from src.api.model_catalog import resolve_model

    db = _fake_db()
    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=_CATALOG["claude-sonnet-4-6"])):
        result = await resolve_model(db, "claude-sonnet-4-6")

    assert result["model"] == "claude-sonnet-4-6"
    assert result["provider"] == "anthropic"
    assert result["api_key"] == "test-anthropic-key"
    assert result["base_url"] is None


@pytest.mark.asyncio
async def test_resolve_model_deepseek(monkeypatch):
    """resolve_model returns correct deepseek credentials."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    from src.api.model_catalog import resolve_model

    db = _fake_db()
    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=_CATALOG["deepseek-v4-flash"])):
        result = await resolve_model(db, "deepseek-v4-flash")

    assert result["model"] == "deepseek-v4-flash"
    assert result["provider"] == "deepseek"
    assert result["api_key"] == "test-deepseek-key"
    assert result["base_url"] == "https://api.deepseek.com"


@pytest.mark.asyncio
async def test_resolve_model_raises_for_unknown_id():
    """resolve_model raises ValueError for an unknown model_id — there is no
    server-side default/fallback to fall back to."""
    from src.api.model_catalog import resolve_model

    db = _fake_db()
    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=None)):
        with pytest.raises(ValueError, match="Unknown or inactive model"):
            await resolve_model(db, "nonexistent-model-xyz")


@pytest.mark.asyncio
async def test_resolve_model_raises_for_inactive_id():
    """resolve_model raises ValueError for a model that exists but is inactive."""
    from src.api.model_catalog import resolve_model

    db = _fake_db()
    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=_INACTIVE)):
        with pytest.raises(ValueError, match="Unknown or inactive model"):
            await resolve_model(db, "old-model")


@pytest.mark.asyncio
async def test_resolve_model_raises_for_empty_id():
    """resolve_model raises ValueError when model_id is empty."""
    from src.api.model_catalog import resolve_model

    db = _fake_db()
    with pytest.raises(ValueError, match="model is required"):
        await resolve_model(db, "")


# ---------------------------------------------------------------------------
# Store method tests for model_catalog CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_catalog_model_clears_previous_default():
    """Setting is_default=True on a model clears the previous default via store update."""
    from src.db import store as db_store

    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    existing = _FakeCatalogRow("claude-opus-4-8", "Claude Opus 4.8", "anthropic", True, False)

    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=existing)):
        result = await db_store.update_catalog_model(db, "claude-opus-4-8", is_default=True)

    # db.execute should have been called to clear the old default (UPDATE SET is_default=False)
    assert db.execute.called
    assert result is not None
    assert result.is_default is True


@pytest.mark.asyncio
async def test_update_catalog_model_rejects_deactivating_current_default():
    """Cannot deactivate a model that is currently the default — ValueError raised."""
    from src.db import store as db_store

    db = MagicMock()
    current_default = _FakeCatalogRow("claude-sonnet-4-6", "Claude Sonnet 4.6", "anthropic", True, True)

    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=current_default)):
        with pytest.raises(ValueError, match="default"):
            await db_store.update_catalog_model(db, "claude-sonnet-4-6", is_active=False)


@pytest.mark.asyncio
async def test_update_catalog_model_returns_none_for_missing_model():
    """update_catalog_model returns None when model_id does not exist."""
    from src.db import store as db_store

    db = MagicMock()
    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=None)):
        result = await db_store.update_catalog_model(db, "nonexistent", display_name="New Name")

    assert result is None


# ---------------------------------------------------------------------------
# Admin route tests via FastAPI TestClient
# ---------------------------------------------------------------------------


def _make_app_with_mock_db(catalog_rows: Optional[List[_FakeCatalogRow]] = None):
    """Build a minimal FastAPI app with admin_models router and mocked DB."""
    from fastapi import FastAPI
    from src.api.routers.admin_models import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    # Inject a mock db_session into app.state
    mock_sessionmaker = MagicMock()
    mock_db = AsyncMock()
    mock_sessionmaker.return_value.__aenter__ = AsyncMock(return_value=mock_db)
    mock_sessionmaker.return_value.__aexit__ = AsyncMock(return_value=False)
    app.state.db_session = mock_sessionmaker

    return app, mock_db


@pytest.mark.asyncio
async def test_admin_list_models_requires_platform_admin():
    """GET /admin/models returns 403 when the caller is not a platform admin."""
    from fastapi import FastAPI
    from src.api.routers.admin_models import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.db_session = MagicMock()

    with (
        patch("src.api.identity.require_identity") as mock_identity,
        patch("src.services.platform_role_client.has_role", new=AsyncMock(return_value=False)),
    ):
        from src.api.identity import Identity
        mock_identity.return_value = Identity(user_id="regular-user", org_id="org-1")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/api/v1/admin/models", headers={"Authorization": "Bearer tok"})

    assert response.status_code == 403


@pytest.mark.asyncio
async def test_admin_list_models_returns_catalog():
    """GET /admin/models returns all catalog rows when caller is platform admin."""
    from fastapi import FastAPI
    from src.api.routers.admin_models import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.db_session = MagicMock()

    catalog_data = [
        {
            "model_id": "claude-sonnet-4-6",
            "display_name": "Claude Sonnet 4.6",
            "provider": "anthropic",
            "is_active": True,
            "is_default": True,
            "created_at": None,
            "updated_at": None,
        }
    ]

    with (
        patch("src.api.identity.require_identity") as mock_identity,
        patch("src.services.platform_role_client.has_role", new=AsyncMock(return_value=True)),
        patch("src.db.store.list_catalog_models", new=AsyncMock(return_value=catalog_data)),
    ):
        from src.api.identity import Identity
        mock_identity.return_value = Identity(user_id="admin-user", org_id="org-1")

        client = TestClient(app, raise_server_exceptions=True)
        response = client.get("/api/v1/admin/models", headers={"Authorization": "Bearer tok"})

    assert response.status_code == 200
    body = response.json()
    assert "models" in body
    assert len(body["models"]) == 1
    assert body["models"][0]["model_id"] == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_admin_create_model_rejects_unknown_provider():
    """POST /admin/models rejects a model with an unsupported provider."""
    from fastapi import FastAPI
    from src.api.routers.admin_models import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.db_session = MagicMock()

    with (
        patch("src.api.identity.require_identity") as mock_identity,
        patch("src.services.platform_role_client.has_role", new=AsyncMock(return_value=True)),
    ):
        from src.api.identity import Identity
        mock_identity.return_value = Identity(user_id="admin-user", org_id="org-1")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.post(
            "/api/v1/admin/models",
            json={"model_id": "gpt-4", "display_name": "GPT-4", "provider": "openai"},
            headers={"Authorization": "Bearer tok"},
        )

    assert response.status_code == 422  # pydantic validation error


@pytest.mark.asyncio
async def test_admin_create_model_success():
    """POST /admin/models creates a model with valid data."""
    from fastapi import FastAPI
    from src.api.routers.admin_models import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.db_session = MagicMock()

    new_row = _FakeCatalogRow("claude-new-model", "Claude New", "anthropic", True, False)

    with (
        patch("src.api.identity.require_identity") as mock_identity,
        patch("src.services.platform_role_client.has_role", new=AsyncMock(return_value=True)),
        patch("src.db.store.create_catalog_model", new=AsyncMock(return_value=new_row)),
    ):
        from src.api.identity import Identity
        mock_identity.return_value = Identity(user_id="admin-user", org_id="org-1")

        client = TestClient(app, raise_server_exceptions=True)
        response = client.post(
            "/api/v1/admin/models",
            json={"model_id": "claude-new-model", "display_name": "Claude New", "provider": "anthropic"},
            headers={"Authorization": "Bearer tok"},
        )

    assert response.status_code == 201
    body = response.json()
    assert body["model_id"] == "claude-new-model"
    assert body["provider"] == "anthropic"


@pytest.mark.asyncio
async def test_admin_patch_model_not_found():
    """PATCH /admin/models/{id} returns 404 when model does not exist."""
    from fastapi import FastAPI
    from src.api.routers.admin_models import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.db_session = MagicMock()

    with (
        patch("src.api.identity.require_identity") as mock_identity,
        patch("src.services.platform_role_client.has_role", new=AsyncMock(return_value=True)),
        patch("src.db.store.update_catalog_model", new=AsyncMock(return_value=None)),
    ):
        from src.api.identity import Identity
        mock_identity.return_value = Identity(user_id="admin-user", org_id="org-1")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.patch(
            "/api/v1/admin/models/nonexistent",
            json={"display_name": "New Name"},
            headers={"Authorization": "Bearer tok"},
        )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_admin_patch_model_rejects_deactivating_default():
    """PATCH /admin/models/{id} returns 400 when trying to deactivate the current default."""
    from fastapi import FastAPI
    from src.api.routers.admin_models import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.db_session = MagicMock()

    with (
        patch("src.api.identity.require_identity") as mock_identity,
        patch("src.services.platform_role_client.has_role", new=AsyncMock(return_value=True)),
        patch("src.db.store.update_catalog_model", new=AsyncMock(side_effect=ValueError("Cannot deactivate the current default model"))),
    ):
        from src.api.identity import Identity
        mock_identity.return_value = Identity(user_id="admin-user", org_id="org-1")

        client = TestClient(app, raise_server_exceptions=False)
        response = client.patch(
            "/api/v1/admin/models/claude-sonnet-4-6",
            json={"is_active": False},
            headers={"Authorization": "Bearer tok"},
        )

    assert response.status_code == 400
    assert "default" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_admin_patch_model_set_new_default_clears_old():
    """PATCH /admin/models/{id} with is_default=True clears the previous default (store layer)."""
    from src.db import store as db_store

    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    # The model to be set as default (not currently default)
    row = _FakeCatalogRow("claude-opus-4-8", "Claude Opus 4.8", "anthropic", True, False)

    with patch("src.db.store.get_catalog_model", new=AsyncMock(return_value=row)):
        result = await db_store.update_catalog_model(db, "claude-opus-4-8", is_default=True)

    # Should have called db.execute to clear existing defaults (UPDATE is_default=False)
    assert db.execute.call_count >= 1
    assert result.is_default is True


@pytest.mark.asyncio
async def test_admin_patch_model_success():
    """PATCH /admin/models/{id} successfully updates a model's display_name."""
    from fastapi import FastAPI
    from src.api.routers.admin_models import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.db_session = MagicMock()

    updated_row = _FakeCatalogRow("claude-sonnet-4-6", "Claude Sonnet 4.6 (Updated)", "anthropic", True, True)

    with (
        patch("src.api.identity.require_identity") as mock_identity,
        patch("src.services.platform_role_client.has_role", new=AsyncMock(return_value=True)),
        patch("src.db.store.update_catalog_model", new=AsyncMock(return_value=updated_row)),
    ):
        from src.api.identity import Identity
        mock_identity.return_value = Identity(user_id="admin-user", org_id="org-1")

        client = TestClient(app, raise_server_exceptions=True)
        response = client.patch(
            "/api/v1/admin/models/claude-sonnet-4-6",
            json={"display_name": "Claude Sonnet 4.6 (Updated)"},
            headers={"Authorization": "Bearer tok"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["display_name"] == "Claude Sonnet 4.6 (Updated)"
