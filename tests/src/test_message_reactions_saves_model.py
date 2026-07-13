"""Unit tests for T1 (m3-agent-chat-essential-feature): DB model additions.

Covers:
- MessageReaction model: column types, nullability, defaults.
- MessageSave model: composite PK columns, types, nullability.
- Message.edited_at and Message.forwarded_from_message_id new columns.
- Index declarations on MessageReaction and MessageSave.
- Migration SQL file exists and contains the expected DDL statements.
- Backward-compatibility: existing Message construction is unaffected.
"""

from __future__ import annotations

import os
import time


# ---------------------------------------------------------------------------
# MessageReaction model
# ---------------------------------------------------------------------------


def test_message_reaction_has_id_column():
    from src.db.models import MessageReaction

    col = MessageReaction.id.property.columns[0]
    assert str(col.type) == "BIGINT"
    assert col.primary_key is True


def test_message_reaction_has_message_id_column():
    from src.db.models import MessageReaction

    col = MessageReaction.message_id.property.columns[0]
    assert str(col.type) == "BIGINT"
    assert col.nullable is False


def test_message_reaction_has_user_id_column():
    from src.db.models import MessageReaction

    col = MessageReaction.user_id.property.columns[0]
    assert col.nullable is False


def test_message_reaction_has_emoji_column():
    from src.db.models import MessageReaction

    col = MessageReaction.emoji.property.columns[0]
    assert col.nullable is False


def test_message_reaction_has_created_at_column():
    from src.db.models import MessageReaction

    col = MessageReaction.created_at.property.columns[0]
    assert col.nullable is False
    # SQLAlchemy renders Double as "DOUBLE" in str()
    assert "DOUBLE" in str(col.type).upper()


def test_message_reaction_construction():
    from src.db.models import MessageReaction

    now = time.time()
    r = MessageReaction(message_id=1, user_id="user-1", emoji="👀", created_at=now)
    assert r.message_id == 1
    assert r.user_id == "user-1"
    assert r.emoji == "👀"
    assert r.created_at == now
    assert r.id is None  # not set until DB assigns it


def test_message_reaction_tablename():
    from src.db.models import MessageReaction

    assert MessageReaction.__tablename__ == "message_reactions"


def test_message_reaction_indexes():
    from src.db.models import MessageReaction

    index_names = {
        idx.name for idx in MessageReaction.__table_args__ if hasattr(idx, "name")
    }
    assert "idx_message_reactions_message" in index_names
    assert "uq_message_reactions_user_emoji" in index_names


def test_message_reaction_unique_index_columns():
    from src.db.models import MessageReaction

    idx = next(
        (
            i
            for i in MessageReaction.__table_args__
            if getattr(i, "name", None) == "uq_message_reactions_user_emoji"
        ),
        None,
    )
    assert idx is not None
    assert idx.unique is True
    col_names = [c.name for c in idx.columns]
    assert col_names == ["message_id", "user_id", "emoji"]


def test_message_reaction_message_index_columns():
    from src.db.models import MessageReaction

    idx = next(
        (
            i
            for i in MessageReaction.__table_args__
            if getattr(i, "name", None) == "idx_message_reactions_message"
        ),
        None,
    )
    assert idx is not None
    col_names = [c.name for c in idx.columns]
    assert col_names == ["message_id"]


# ---------------------------------------------------------------------------
# MessageSave model
# ---------------------------------------------------------------------------


def test_message_save_has_message_id_column():
    from src.db.models import MessageSave

    col = MessageSave.message_id.property.columns[0]
    assert str(col.type) == "BIGINT"
    assert col.primary_key is True
    assert col.nullable is False


def test_message_save_has_user_id_column():
    from src.db.models import MessageSave

    col = MessageSave.user_id.property.columns[0]
    assert col.primary_key is True
    assert col.nullable is False


def test_message_save_has_saved_at_column():
    from src.db.models import MessageSave

    col = MessageSave.saved_at.property.columns[0]
    assert col.nullable is False
    assert "DOUBLE" in str(col.type).upper()


def test_message_save_tablename():
    from src.db.models import MessageSave

    assert MessageSave.__tablename__ == "message_saves"


def test_message_save_construction():
    from src.db.models import MessageSave

    now = time.time()
    s = MessageSave(message_id=42, user_id="user-7", saved_at=now)
    assert s.message_id == 42
    assert s.user_id == "user-7"
    assert s.saved_at == now


def test_message_save_index():
    from src.db.models import MessageSave

    index_names = {
        idx.name for idx in MessageSave.__table_args__ if hasattr(idx, "name")
    }
    assert "idx_message_saves_user" in index_names


def test_message_save_index_columns():
    from src.db.models import MessageSave

    idx = next(
        (
            i
            for i in MessageSave.__table_args__
            if getattr(i, "name", None) == "idx_message_saves_user"
        ),
        None,
    )
    assert idx is not None
    col_names = [c.name for c in idx.columns]
    assert col_names == ["user_id", "saved_at"]


# ---------------------------------------------------------------------------
# Message model: new edited_at and forwarded_from_message_id columns
# ---------------------------------------------------------------------------


def test_message_has_edited_at_column():
    from src.db.models import Message

    col = Message.edited_at.property.columns[0]
    assert col.nullable is True
    assert "DOUBLE" in str(col.type).upper()


def test_message_has_forwarded_from_message_id_column():
    from src.db.models import Message

    col = Message.forwarded_from_message_id.property.columns[0]
    assert col.nullable is True
    assert str(col.type) == "BIGINT"


def test_message_edited_at_defaults_to_none():
    from src.db.models import Message

    msg = Message(session_id="s1", role="user", content="hi", created_at=0.0)
    assert msg.edited_at is None


def test_message_forwarded_from_defaults_to_none():
    from src.db.models import Message

    msg = Message(session_id="s1", role="user", content="hi", created_at=0.0)
    assert msg.forwarded_from_message_id is None


def test_message_new_columns_settable():
    from src.db.models import Message

    now = time.time()
    msg = Message(
        session_id="s1",
        role="user",
        content="edited content",
        created_at=now,
        edited_at=now,
        forwarded_from_message_id=99,
    )
    assert msg.edited_at == now
    assert msg.forwarded_from_message_id == 99


# ---------------------------------------------------------------------------
# Backward-compatibility: existing Message construction unaffected
# ---------------------------------------------------------------------------


def test_existing_message_construction_unaffected():
    from src.db.models import Message

    now = time.time()
    msg = Message(
        session_id="sess-legacy",
        role="user",
        content="hello",
        author_id="user-1",
        created_at=now,
        reply_to_message_id=5,
        thread_root_id=3,
    )
    assert msg.session_id == "sess-legacy"
    assert msg.reply_to_message_id == 5
    assert msg.thread_root_id == 3
    assert msg.edited_at is None
    assert msg.forwarded_from_message_id is None


# ---------------------------------------------------------------------------
# Migration SQL file existence and key DDL presence
# ---------------------------------------------------------------------------


def test_migration_file_exists():
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    migration_path = os.path.join(
        repo_root, "migrations", "011_message_reactions_saves.sql"
    )
    assert os.path.isfile(migration_path), f"Migration file not found: {migration_path}"


def test_migration_creates_message_reactions_table():
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    migration_path = os.path.join(
        repo_root, "migrations", "011_message_reactions_saves.sql"
    )
    content = open(migration_path).read()
    assert "message_reactions" in content
    assert "CREATE TABLE" in content


def test_migration_creates_message_saves_table():
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    migration_path = os.path.join(
        repo_root, "migrations", "011_message_reactions_saves.sql"
    )
    content = open(migration_path).read()
    assert "message_saves" in content


def test_migration_adds_edited_at_column():
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    migration_path = os.path.join(
        repo_root, "migrations", "011_message_reactions_saves.sql"
    )
    content = open(migration_path).read()
    assert "edited_at" in content
    assert "ALTER TABLE messages" in content


def test_migration_adds_forwarded_from_message_id_column():
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    migration_path = os.path.join(
        repo_root, "migrations", "011_message_reactions_saves.sql"
    )
    content = open(migration_path).read()
    assert "forwarded_from_message_id" in content


def test_migration_wrapped_in_transaction():
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    migration_path = os.path.join(
        repo_root, "migrations", "011_message_reactions_saves.sql"
    )
    content = open(migration_path).read()
    assert "BEGIN;" in content
    assert "COMMIT;" in content


def test_migration_unique_index_on_reactions():
    repo_root = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    migration_path = os.path.join(
        repo_root, "migrations", "011_message_reactions_saves.sql"
    )
    content = open(migration_path).read()
    assert "UNIQUE" in content
    assert "uq_message_reactions_user_emoji" in content
