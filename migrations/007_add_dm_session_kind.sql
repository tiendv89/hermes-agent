-- agent-general-chat: widen sessions.kind to support 1:1 Direct Message sessions (G2).
-- Additive migration: no new tables, no column changes. Adds 'dm' to the
-- kind check constraint and a session_members lookup index.

BEGIN;

ALTER TABLE sessions
    DROP CONSTRAINT IF EXISTS sessions_kind_check;

ALTER TABLE sessions
    ADD CONSTRAINT sessions_kind_check
    CHECK (kind IN ('session', 'thread', 'channel', 'dm'));

CREATE INDEX IF NOT EXISTS idx_session_members_session_member
    ON session_members (session_id, user_id);

COMMIT;
