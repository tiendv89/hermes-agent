"""Catalog of user-selectable chat models and how to run each one.

The workflow gateway exposes this list via ``GET /api/v1/models`` so the
front-end can render a model picker, and resolves a chosen model id to the
provider + credentials used to construct the agent for a turn.

The agent itself accepts arbitrary model strings; this module is the curated,
*supported* subset (all current-gen Claude + DeepSeek models) plus the
provider/credential wiring for each. Add a model here to make it selectable.
"""

from __future__ import annotations

import os
from typing import Optional, TypedDict


class ModelInfo(TypedDict):
    id: str
    label: str
    provider: str


# Current-generation Claude models, run through Anthropic.
CLAUDE_MODELS: list[ModelInfo] = [
    {"id": "claude-opus-4-8", "label": "Claude Opus 4.8", "provider": "anthropic"},
    {"id": "claude-sonnet-4-6", "label": "Claude Sonnet 4.6", "provider": "anthropic"},
    {"id": "claude-haiku-4-5", "label": "Claude Haiku 4.5", "provider": "anthropic"},
]

# DeepSeek models, run through DeepSeek's OpenAI-compatible API.
DEEPSEEK_MODELS: list[ModelInfo] = [
    {"id": "deepseek-chat", "label": "DeepSeek V3", "provider": "deepseek"},
    {"id": "deepseek-reasoner", "label": "DeepSeek R1", "provider": "deepseek"},
    {"id": "deepseek-v4-flash", "label": "DeepSeek V4 Flash", "provider": "deepseek"},
    {"id": "deepseek-v4-pro", "label": "DeepSeek V4 Pro", "provider": "deepseek"},
]

SUPPORTED_MODELS: list[ModelInfo] = [*CLAUDE_MODELS, *DEEPSEEK_MODELS]

_BY_ID: dict[str, ModelInfo] = {m["id"]: m for m in SUPPORTED_MODELS}

# Fallback when neither the request nor HERMES_MODEL names a supported model.
_FALLBACK_MODEL = "claude-sonnet-4-6"


def is_supported(model_id: str) -> bool:
    return model_id in _BY_ID


def default_model() -> str:
    """The server default — HERMES_MODEL if it names a supported model, else the fallback."""
    env = os.environ.get("HERMES_MODEL", "").strip()
    return env if env in _BY_ID else _FALLBACK_MODEL


class ResolvedModel(TypedDict):
    model: str
    provider: str
    api_key: Optional[str]
    base_url: Optional[str]


def resolve_model(model_id: str) -> ResolvedModel:
    """Map a catalog model id to the provider + credentials used to run it.

    An unknown id (e.g. a stale FE selection) falls back to the server default
    so a turn can never be wedged by a bad model string.
    """
    info = _BY_ID.get((model_id or "").strip()) or _BY_ID[default_model()]
    provider = info["provider"]

    if provider == "deepseek":
        return {
            "model": info["id"],
            "provider": "deepseek",
            "api_key": os.environ.get("DEEPSEEK_API_KEY") or None,
            "base_url": os.environ.get("DEEPSEEK_BASE_URL") or None,
        }

    # Default / anthropic: Anthropic Messages API, key from ANTHROPIC_API_KEY.
    return {
        "model": info["id"],
        "provider": provider,
        "api_key": os.environ.get("ANTHROPIC_API_KEY") or None,
        "base_url": None,
    }
