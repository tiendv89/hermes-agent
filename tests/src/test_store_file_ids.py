"""Tests for the file_ids plumbing added alongside image_ids (chat-file-upload T3).

Covers:
- append_message passes file_ids to Message as a real list, not a JSON string.
- append_message defaults file_ids to [] (not None, not "[]").
- _file_urls_for handles a real list (the correct/fixed shape).
- _file_urls_for defensively re-parses a JSON-encoded string (legacy rows).
- _file_urls_for degrades to [] on unparseable garbage rather than raising.
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
# append_message — file_ids passed as a native list
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_append_message_passes_file_ids_as_a_real_list_not_json_string():
    from src.db.store import append_message

    db = _mock_db()
    captured = []
    db.add.side_effect = lambda obj: captured.append(obj)

    async def _flush_set_id():
        if captured:
            captured[-1].id = 42

    db.flush.side_effect = _flush_set_id

    with patch("src.db.store.mark_session_read", new_callable=AsyncMock):
        with patch("src.db.store._emit_message_notifications", new_callable=AsyncMock):
            await append_message(
                db,
                session_id="sess-1",
                role="user",
                content="check this report",
                author_id="user-1",
                file_ids=["file-1", "file-2"],
            )

    assert len(captured) == 1
    msg = captured[0]
    assert isinstance(msg.file_ids, list), (
        f"file_ids must be a real list for the JSONB column, got {type(msg.file_ids)!r}: {msg.file_ids!r}"
    )
    assert msg.file_ids == ["file-1", "file-2"]


@pytest.mark.asyncio
async def test_append_message_without_file_ids_defaults_to_empty_list():
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
    assert msg.file_ids == []
    assert isinstance(msg.file_ids, list)


@pytest.mark.asyncio
async def test_append_message_passes_both_image_and_file_ids():
    """A message can have both image_ids and file_ids simultaneously."""
    from src.db.store import append_message

    db = _mock_db()
    captured = []
    db.add.side_effect = lambda obj: captured.append(obj)

    async def _flush_set_id():
        if captured:
            captured[-1].id = 44

    db.flush.side_effect = _flush_set_id

    with patch("src.db.store.mark_session_read", new_callable=AsyncMock):
        with patch("src.db.store._emit_message_notifications", new_callable=AsyncMock):
            await append_message(
                db,
                session_id="sess-1",
                role="user",
                content="images and files",
                author_id="user-1",
                image_ids=["img-1"],
                file_ids=["file-1"],
            )

    msg = captured[0]
    assert msg.image_ids == ["img-1"]
    assert msg.file_ids == ["file-1"]


# ---------------------------------------------------------------------------
# _file_urls_for — URL construction and defensive re-parsing
# ---------------------------------------------------------------------------


def test_file_urls_for_handles_a_real_list():
    from src.api.routers.messages import _file_urls_for

    urls = _file_urls_for("ws-1", ["file-1", "file-2"])
    assert urls == [
        "/api/workspaces/ws-1/files/file-1",
        "/api/workspaces/ws-1/files/file-2",
    ]


def test_file_urls_for_reparses_a_double_encoded_json_string():
    """Legacy rows written before the fix come back as a JSON string."""
    from src.api.routers.messages import _file_urls_for

    corrupted = '["b41b2894-d3b3-4185-8dd8-3d7d75d34cc2"]'
    urls = _file_urls_for("ws-1", corrupted)
    assert urls == ["/api/workspaces/ws-1/files/b41b2894-d3b3-4185-8dd8-3d7d75d34cc2"]


def test_file_urls_for_returns_empty_list_on_unparseable_garbage():
    from src.api.routers.messages import _file_urls_for

    assert _file_urls_for("ws-1", "not valid json") == []
    assert _file_urls_for("ws-1", "{}") == []  # valid JSON, but not a list
    assert _file_urls_for("ws-1", None) == []
    assert _file_urls_for("ws-1", []) == []
