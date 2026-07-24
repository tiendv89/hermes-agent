"""Regression tests for the message.image_ids double-JSON-encoding bug.

append_message used to call json.dumps() on the image_ids list before
assigning it to the ORM's JSONB column — but SQLAlchemy's JSONB dialect
already serializes a native Python list on write, so pre-dumping it produced
a JSON string *containing* JSON text. Reading that back gave a plain string
instead of a list, and any code iterating it (e.g. building per-image URLs
in messages.py's _image_urls_for) walked it character-by-character, emitting
one bogus one-character "image" per character in the UUID.

Covers:
- append_message passes image_ids to Message as a real list, not a JSON string.
- append_message defaults image_ids to [] (not None, not "[]").
- _image_urls_for handles a real list (the correct/fixed shape).
- _image_urls_for defensively re-parses a JSON-encoded string (an
  already-corrupted legacy row) instead of iterating it character-by-character.
- _image_urls_for degrades to [] on unparseable garbage rather than raising.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _mock_db():
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    db.delete = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# append_message — image_ids passed as a native list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_message_passes_image_ids_as_a_real_list_not_json_string():
    from src.db.store import append_message

    db = _mock_db()
    captured = []
    db.add.side_effect = lambda obj: captured.append(obj)

    async def _flush_set_id():
        if captured:
            captured[-1].id = 42

    db.flush.side_effect = _flush_set_id

    with (
        patch("src.db.store.mark_session_read", new_callable=AsyncMock),
        patch("src.db.store._emit_message_notifications", new_callable=AsyncMock),
    ):
        await append_message(
            db,
            session_id="sess-1",
            role="user",
            content="what's in this image?",
            author_id="user-1",
            image_ids=["img-1", "img-2"],
        )

    assert len(captured) == 1
    msg = captured[0]
    assert isinstance(msg.image_ids, list), (
        f"image_ids must be a real list for the JSONB column, got {type(msg.image_ids)!r}: {msg.image_ids!r}"
    )
    assert msg.image_ids == ["img-1", "img-2"]


@pytest.mark.asyncio
async def test_append_message_without_image_ids_defaults_to_empty_list():
    from src.db.store import append_message

    db = _mock_db()
    captured = []
    db.add.side_effect = lambda obj: captured.append(obj)

    async def _flush_set_id():
        if captured:
            captured[-1].id = 43

    db.flush.side_effect = _flush_set_id

    with patch("src.db.store.mark_session_read", new_callable=AsyncMock):
        await append_message(db, session_id="sess-1", role="assistant", content="hi")

    msg = captured[0]
    assert msg.image_ids == []
    assert isinstance(msg.image_ids, list)


# ---------------------------------------------------------------------------
# _image_urls_for — defensive re-parsing of legacy double-encoded rows
# ---------------------------------------------------------------------------


def test_image_urls_for_handles_a_real_list():
    from src.api.routers.messages import _image_urls_for

    urls = _image_urls_for("ws-1", ["img-1", "img-2"])
    assert urls == [
        "/api/workspaces/ws-1/images/img-1",
        "/api/workspaces/ws-1/images/img-2",
    ]


def test_image_urls_for_reparses_a_double_encoded_json_string():
    """Legacy rows written before the fix come back as a JSON string, e.g.
    '["b41b2894-d3b3-4185-8dd8-3d7d75d34cc2"]' — must still resolve to one
    URL, not one URL per character."""
    from src.api.routers.messages import _image_urls_for

    corrupted = '["b41b2894-d3b3-4185-8dd8-3d7d75d34cc2"]'
    urls = _image_urls_for("ws-1", corrupted)
    assert urls == ["/api/workspaces/ws-1/images/b41b2894-d3b3-4185-8dd8-3d7d75d34cc2"]


def test_image_urls_for_returns_empty_list_on_unparseable_garbage():
    from src.api.routers.messages import _image_urls_for

    assert _image_urls_for("ws-1", "not valid json") == []
    assert _image_urls_for("ws-1", "{}") == []  # valid JSON, but not a list
    assert _image_urls_for("ws-1", None) == []
