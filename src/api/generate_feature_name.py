"""Generate feature name slug endpoint.

POST /generate-feature-name — lightweight, non-streaming endpoint that sends a
single-turn LLM completion and returns a generated kebab-case slug.
"""

from __future__ import annotations

import logging
import os
import re
from functools import lru_cache

import anthropic
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.identity import Identity, require_identity

logger = logging.getLogger(__name__)

router = APIRouter()

_SYSTEM_PROMPT = (
    "Generate a short kebab-case feature name slug (3-5 words, lowercase, hyphens only) "
    "from this description. Extract the meaningful domain words — skip filler words like "
    "'the', 'a', 'fix', 'need', 'should'. Return ONLY the slug, no other text."
)

_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 64


class GenerateFeatureNameRequest(BaseModel):
    description: str


@lru_cache(maxsize=1)
def _get_anthropic_client(api_key: str) -> anthropic.AsyncAnthropic:
    """Return a cached AsyncAnthropic client for the given API key.

    Cached with maxsize=1 so a single client instance is reused across
    requests that share the same API key, avoiding new connection-pool
    creation on every invocation.
    """
    return anthropic.AsyncAnthropic(api_key=api_key)


def _sanitize_slug(raw: str) -> str:
    """Normalize LLM output into a valid kebab-case slug.

    Strips leading/trailing non-alphanumeric characters, replaces runs of
    non-alphanumeric characters with a single hyphen, and lowercases.
    """
    slug = raw.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug


@router.post("/generate-feature-name")
async def generate_feature_name_endpoint(
    body: GenerateFeatureNameRequest,
    identity: Identity = Depends(require_identity),
) -> JSONResponse:
    """Return an LLM-generated kebab-case slug from a feature description."""
    description = (body.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required and must not be empty.")

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise HTTPException(status_code=503, detail="LLM service unavailable: ANTHROPIC_API_KEY not configured.")

    model = os.environ.get("GENERATE_FEATURE_NAME_MODEL", _DEFAULT_MODEL)

    try:
        client = _get_anthropic_client(api_key)
        message = await client.messages.create(
            model=model,
            max_tokens=_MAX_TOKENS,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": description}],
        )
        raw = message.content[0].text
        slug = _sanitize_slug(raw)
    except Exception as exc:
        logger.warning("generate_feature_name: LLM call failed: %s", exc)
        raise HTTPException(status_code=503, detail="LLM service temporarily unavailable.") from exc

    return JSONResponse({"name": slug})
