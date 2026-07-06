-- notification-service: per-user "last read" cursor per session, powering a
-- general unread-message-count badge (not just @mentions). Decoupled from
-- session_members so it works uniformly for channels, DMs, and threads even
-- when the viewer has no explicit membership row (e.g. a thread's owner).

BEGIN;

CREATE TABLE IF NOT EXISTS session_reads (
    session_id              UUID             NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    user_id                 TEXT             NOT NULL,
    last_read_message_count INTEGER          NOT NULL DEFAULT 0,
    updated_at              DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (session_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_session_reads_user ON session_reads(user_id);

COMMIT;
