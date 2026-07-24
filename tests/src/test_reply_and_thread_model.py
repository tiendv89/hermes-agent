"""Unit tests for T1: reply_to_message_id / thread_root_id columns on Message.

Covers:
- Both columns exist on the Message model with the correct type and nullability.
- Both columns default to None (NULL) on a newly-constructed instance.
- Legacy rows (no reply/thread args) are unaffected — all existing fields still
  work correctly.
- The new indexes are declared in Message.__table_args__.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Column existence and default-NULL behaviour
# ---------------------------------------------------------------------------


def test_message_has_reply_to_message_id_column():
    from src.db.models import Message

    col = Message.reply_to_message_id.property.columns[0]
    assert col.nullable is True, "reply_to_message_id must be nullable"
    assert str(col.type) == "BIGINT", f"Expected BIGINT, got {col.type}"


def test_message_has_thread_root_id_column():
    from src.db.models import Message

    col = Message.thread_root_id.property.columns[0]
    assert col.nullable is True, "thread_root_id must be nullable"
    assert str(col.type) == "BIGINT", f"Expected BIGINT, got {col.type}"


def test_reply_to_message_id_defaults_to_none():
    from src.db.models import Message

    msg = Message(session_id="s1", role="user", content="hi", created_at=0.0)
    assert msg.reply_to_message_id is None


def test_thread_root_id_defaults_to_none():
    from src.db.models import Message

    msg = Message(session_id="s1", role="user", content="hi", created_at=0.0)
    assert msg.thread_root_id is None


# ---------------------------------------------------------------------------
# Backward-compatibility: existing columns are unaffected
# ---------------------------------------------------------------------------


def test_legacy_message_construction_still_works():
    """Constructing a Message without the new kwargs mirrors legacy behaviour."""
    import time

    from src.db.models import Message

    now = time.time()
    msg = Message(
        session_id="sess-legacy",
        role="user",
        content="hello world",
        author_id="user-42",
        created_at=now,
    )

    assert msg.session_id == "sess-legacy"
    assert msg.role == "user"
    assert msg.content == "hello world"
    assert msg.author_id == "user-42"
    assert msg.reply_to_message_id is None
    assert msg.thread_root_id is None


def test_message_construction_with_reply_fields():
    """Setting both new fields is accepted and round-trips correctly."""
    import time

    from src.db.models import Message

    now = time.time()
    msg = Message(
        session_id="sess-1",
        role="user",
        content="replying here",
        author_id="user-1",
        created_at=now,
        reply_to_message_id=99,
        thread_root_id=50,
    )

    assert msg.reply_to_message_id == 99
    assert msg.thread_root_id == 50


def test_message_with_only_reply_to_no_thread():
    """reply_to_message_id set, thread_root_id NULL — inline reply in main transcript."""
    import time

    from src.db.models import Message

    msg = Message(
        session_id="sess-1",
        role="user",
        content="inline reply",
        created_at=time.time(),
        reply_to_message_id=10,
    )

    assert msg.reply_to_message_id == 10
    assert msg.thread_root_id is None


def test_message_with_only_thread_root():
    """thread_root_id set, reply_to_message_id NULL — first reply in a thread."""
    import time

    from src.db.models import Message

    msg = Message(
        session_id="sess-1",
        role="user",
        content="first thread reply",
        created_at=time.time(),
        thread_root_id=7,
    )

    assert msg.thread_root_id == 7
    assert msg.reply_to_message_id is None


# ---------------------------------------------------------------------------
# Index declarations
# ---------------------------------------------------------------------------


def test_indexes_declared_in_table_args():
    from src.db.models import Message

    index_names = {idx.name for idx in Message.__table_args__ if hasattr(idx, "name")}
    assert "idx_messages_thread_root" in index_names, (
        "idx_messages_thread_root must be in Message.__table_args__"
    )
    assert "idx_messages_reply_to" in index_names, (
        "idx_messages_reply_to must be in Message.__table_args__"
    )


def test_thread_root_index_covers_correct_columns():
    from src.db.models import Message

    idx = next(
        (
            i
            for i in Message.__table_args__
            if getattr(i, "name", None) == "idx_messages_thread_root"
        ),
        None,
    )
    assert idx is not None
    col_names = [c.name for c in idx.columns]
    assert col_names == ["session_id", "thread_root_id", "created_at"], (
        f"Unexpected columns in idx_messages_thread_root: {col_names}"
    )


def test_reply_to_index_covers_correct_column():
    from src.db.models import Message

    idx = next(
        (
            i
            for i in Message.__table_args__
            if getattr(i, "name", None) == "idx_messages_reply_to"
        ),
        None,
    )
    assert idx is not None
    col_names = [c.name for c in idx.columns]
    assert col_names == ["reply_to_message_id"], (
        f"Unexpected columns in idx_messages_reply_to: {col_names}"
    )
