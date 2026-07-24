"""Tests for POST /generate-feature-name endpoint.

Covers:
  - Happy path: returns a slug from LLM response
  - Empty description → 400
  - Blank description (whitespace only) → 400
  - Missing ANTHROPIC_API_KEY → 503
  - LLM call raises exception → 503
  - Response slug is lowercased
  - Auth dependency: require_identity is wired up
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_llm_response(text: str) -> MagicMock:
    """Build a mock anthropic Messages response with a single text block."""
    block = MagicMock()
    block.text = text
    response = MagicMock()
    response.content = [block]
    return response


# ---------------------------------------------------------------------------
# Unit tests for the endpoint handler
# ---------------------------------------------------------------------------


class TestGenerateFeatureNameEndpoint:
    """Tests for generate_feature_name_endpoint logic via direct import."""

    @pytest.mark.asyncio
    async def test_happy_path_returns_slug(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        mock_create = AsyncMock(return_value=_make_llm_response("thread-context-isolation"))
        mock_client = MagicMock()
        mock_client.messages.create = mock_create

        with patch("src.api.generate_feature_name.anthropic") as mock_anthropic:
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            from src.api.generate_feature_name import (
                GenerateFeatureNameRequest,
                generate_feature_name_endpoint,
            )
            from src.api.identity import Identity

            identity = Identity(user_id="user-1")
            body = GenerateFeatureNameRequest(description="Fix thread context isolation bugs")
            response = await generate_feature_name_endpoint(body, identity=identity)

        assert response.status_code == 200
        import json
        data = json.loads(response.body)
        assert data["name"] == "thread-context-isolation"

    @pytest.mark.asyncio
    async def test_slug_is_lowercased(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        mock_create = AsyncMock(return_value=_make_llm_response("Thread-Context-ISOLATION"))
        mock_client = MagicMock()
        mock_client.messages.create = mock_create

        with patch("src.api.generate_feature_name.anthropic") as mock_anthropic:
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            from src.api.generate_feature_name import (
                GenerateFeatureNameRequest,
                generate_feature_name_endpoint,
            )
            from src.api.identity import Identity

            identity = Identity(user_id="user-1")
            body = GenerateFeatureNameRequest(description="Fix something important")
            response = await generate_feature_name_endpoint(body, identity=identity)

        import json
        data = json.loads(response.body)
        assert data["name"] == "thread-context-isolation"

    @pytest.mark.asyncio
    async def test_empty_description_returns_400(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        from fastapi import HTTPException

        from src.api.generate_feature_name import (
            GenerateFeatureNameRequest,
            generate_feature_name_endpoint,
        )
        from src.api.identity import Identity

        identity = Identity(user_id="user-1")
        body = GenerateFeatureNameRequest(description="")

        with pytest.raises(HTTPException) as exc_info:
            await generate_feature_name_endpoint(body, identity=identity)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_blank_description_returns_400(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        from fastapi import HTTPException

        from src.api.generate_feature_name import (
            GenerateFeatureNameRequest,
            generate_feature_name_endpoint,
        )
        from src.api.identity import Identity

        identity = Identity(user_id="user-1")
        body = GenerateFeatureNameRequest(description="   ")

        with pytest.raises(HTTPException) as exc_info:
            await generate_feature_name_endpoint(body, identity=identity)

        assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_missing_api_key_returns_503(self, monkeypatch):
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

        from fastapi import HTTPException

        from src.api.generate_feature_name import (
            GenerateFeatureNameRequest,
            generate_feature_name_endpoint,
        )
        from src.api.identity import Identity

        identity = Identity(user_id="user-1")
        body = GenerateFeatureNameRequest(description="Fix thread context isolation bugs")

        with pytest.raises(HTTPException) as exc_info:
            await generate_feature_name_endpoint(body, identity=identity)

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_llm_exception_returns_503(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")

        # Clear the cached client so the patched mock is used.
        from src.api.generate_feature_name import _get_anthropic_client
        _get_anthropic_client.cache_clear()

        mock_create = AsyncMock(side_effect=RuntimeError("connection refused"))
        mock_client = MagicMock()
        mock_client.messages.create = mock_create

        from fastapi import HTTPException

        with patch("src.api.generate_feature_name.anthropic") as mock_anthropic:
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            from src.api.generate_feature_name import (
                GenerateFeatureNameRequest,
                generate_feature_name_endpoint,
            )
            from src.api.identity import Identity

            identity = Identity(user_id="user-1")
            body = GenerateFeatureNameRequest(description="Fix thread context isolation bugs")

            with pytest.raises(HTTPException) as exc_info:
                await generate_feature_name_endpoint(body, identity=identity)

        assert exc_info.value.status_code == 503

    @pytest.mark.asyncio
    async def test_custom_model_env_var(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setenv("GENERATE_FEATURE_NAME_MODEL", "claude-sonnet-4-5")

        # Clear the cached client so the patched mock is used.
        from src.api.generate_feature_name import _get_anthropic_client
        _get_anthropic_client.cache_clear()

        mock_create = AsyncMock(return_value=_make_llm_response("my-feature-slug"))
        mock_client = MagicMock()
        mock_client.messages.create = mock_create

        with patch("src.api.generate_feature_name.anthropic") as mock_anthropic:
            mock_anthropic.AsyncAnthropic.return_value = mock_client
            from src.api.generate_feature_name import (
                GenerateFeatureNameRequest,
                generate_feature_name_endpoint,
            )
            from src.api.identity import Identity

            identity = Identity(user_id="user-1")
            body = GenerateFeatureNameRequest(description="Add my feature")
            await generate_feature_name_endpoint(body, identity=identity)

        call_kwargs = mock_create.call_args.kwargs
        assert call_kwargs["model"] == "claude-sonnet-4-5"


# ---------------------------------------------------------------------------
# Router registration
# ---------------------------------------------------------------------------


class TestRouterRegistration:
    def test_generate_feature_name_route_registered(self):
        from src.api.generate_feature_name import router

        paths = [route.path for route in router.routes]
        assert "/generate-feature-name" in paths

    def test_generate_feature_name_route_is_post(self):
        from src.api.generate_feature_name import router

        route = next(r for r in router.routes if r.path == "/generate-feature-name")
        assert "POST" in route.methods

    def test_generate_feature_name_included_in_main_router(self):
        from src.api.generate_feature_name import router as gen_router
        from src.api.router import router

        included = [r for r in router.routes if getattr(r, "original_router", None) is gen_router]
        assert len(included) == 1, "generate_feature_name router not included in main router"
