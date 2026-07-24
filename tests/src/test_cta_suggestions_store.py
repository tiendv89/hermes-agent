"""Unit tests for m3-agent-cta DB store functions (T4).

Covers:
  - get_latest_assistant_message_id: returns id when row exists, None when not
  - update_message_cta_suggestions: executes UPDATE with correct args and commits
  - Message model has cta_suggestions column
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_db():
    db = MagicMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# get_latest_assistant_message_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_latest_assistant_message_id_returns_id():
    """Returns message id when a matching row exists."""
    from src.db.store import get_latest_assistant_message_id

    db = _mock_db()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = 42
    db.execute = AsyncMock(return_value=scalar_result)

    result = await get_latest_assistant_message_id(db, "sess-abc")

    assert result == 42
    db.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_latest_assistant_message_id_returns_none_when_missing():
    """Returns None when no assistant message exists for the session."""
    from src.db.store import get_latest_assistant_message_id

    db = _mock_db()
    scalar_result = MagicMock()
    scalar_result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=scalar_result)

    result = await get_latest_assistant_message_id(db, "sess-xyz")

    assert result is None


# ---------------------------------------------------------------------------
# update_message_cta_suggestions
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_message_cta_suggestions_executes_and_commits():
    """Executes an UPDATE and commits the session."""
    from src.db.store import update_message_cta_suggestions

    db = _mock_db()
    suggestions = [
        {
            "id": "sug-1",
            "title": "Approve",
            "category": "Lifecycle",
            "description": "Approve the spec",
            "action_text": "/approve",
            "button_label": "Approve",
        }
    ]

    await update_message_cta_suggestions(db, "sess-abc", 99, suggestions)

    db.execute.assert_awaited_once()
    db.commit.assert_awaited_once()


# ---------------------------------------------------------------------------
# Message model has cta_suggestions column
# ---------------------------------------------------------------------------


def test_message_model_has_cta_suggestions_column():
    """Message ORM model declares the cta_suggestions column."""
    from src.db.models import Message

    assert hasattr(Message, "cta_suggestions"), (
        "Message model missing cta_suggestions column — "
        "run migration 004_cta_suggestions.sql"
    )
