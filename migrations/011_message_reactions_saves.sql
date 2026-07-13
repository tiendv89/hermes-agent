-- m3-agent-chat-essential-feature: message reactions, saves, edit/forward support.
-- Adds two new side-tables (message_reactions, message_saves) and two nullable
-- columns on messages (edited_at, forwarded_from_message_id).
-- All changes are additive — existing rows are unaffected by nullable/defaulted columns.

BEGIN;

-- message_reactions: one row per (message, user, emoji) triple.
-- Toggle semantics: INSERT when absent, DELETE when present (handled in API layer).
CREATE TABLE IF NOT EXISTS message_reactions (
    id          BIGSERIAL PRIMARY KEY,
    message_id  BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id     TEXT   NOT NULL,
    emoji       TEXT   NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_message_reactions_message
    ON message_reactions (message_id);

-- Unique constraint enforces at-most-one row per (message, user, emoji) triple,
-- making INSERT ... ON CONFLICT DO NOTHING safe for idempotent add-only paths.
CREATE UNIQUE INDEX IF NOT EXISTS uq_message_reactions_user_emoji
    ON message_reactions (message_id, user_id, emoji);

-- message_saves: per-user bookmark on a message. Composite PK (message_id, user_id)
-- guarantees idempotent save and efficient unsave by PK lookup.
CREATE TABLE IF NOT EXISTS message_saves (
    message_id  BIGINT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    user_id     TEXT   NOT NULL,
    saved_at    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (message_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_message_saves_user
    ON message_saves (user_id, saved_at);

-- edited_at: NULL = never edited; non-NULL = timestamp of last edit.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS edited_at DOUBLE PRECISION;

-- forwarded_from_message_id: NULL = original message; non-NULL = forwarded copy
-- pointing at the source message (immediate source, not ultimate origin).
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS forwarded_from_message_id BIGINT
        REFERENCES messages(id);

COMMIT;
