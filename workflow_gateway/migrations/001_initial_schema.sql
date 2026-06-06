-- workflow_gateway initial schema
-- mirrors swell-hermes voyager_sessions_v4 / voyager_messages_v4

CREATE TABLE IF NOT EXISTS voyager_sessions_v4 (
    session_id      TEXT PRIMARY KEY,
    user_id         TEXT NOT NULL,
    workspace_id    TEXT NOT NULL DEFAULT '',
    feature_id      TEXT NOT NULL DEFAULT '',
    created_at      DOUBLE PRECISION NOT NULL,
    last_active_at  DOUBLE PRECISION NOT NULL,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    metadata        JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS voyager_messages_v4 (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES voyager_sessions_v4(session_id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_vm4_session ON voyager_messages_v4(session_id, created_at);
