-- m3-agent-chat-v4 additive migration
-- Adds members, message authorship, mentions, and channel discriminator.
-- All changes are backward-compatible: new columns are nullable or defaulted,
-- and existing rows are unaffected.

-- 1. sessions.kind — channel discriminator ('thread' | 'channel')
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'thread';

-- 2. Unique channel name per workspace (case-insensitive, non-archived channels only)
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_channel_name_unique
    ON sessions (workspace_id, lower(title))
    WHERE kind = 'channel' AND archived = FALSE;

-- 3. messages.author_id — sender X-User-Id, or 'agent' sentinel for assistant messages
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS author_id TEXT;

CREATE INDEX IF NOT EXISTS idx_messages_author ON messages(session_id, author_id);

-- 4. session_members — explicit membership for threads and channels
CREATE TABLE IF NOT EXISTS session_members (
    session_id  TEXT        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id     TEXT        NOT NULL,
    role_label  TEXT,
    added_by    TEXT        NOT NULL,
    added_at    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (session_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_session_members_user ON session_members(user_id);

-- 5. message_mentions — resolved @mentions in messages
CREATE TABLE IF NOT EXISTS message_mentions (
    id              BIGSERIAL   PRIMARY KEY,
    message_id      BIGINT      NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    session_id      TEXT        NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    mentioned_id    TEXT        NOT NULL,
    mentioned_kind  TEXT        NOT NULL CHECK (mentioned_kind IN ('user', 'agent')),
    read_at         DOUBLE PRECISION
);

CREATE INDEX IF NOT EXISTS idx_message_mentions_session  ON message_mentions(session_id);
CREATE INDEX IF NOT EXISTS idx_message_mentions_user     ON message_mentions(session_id, mentioned_id, read_at);
