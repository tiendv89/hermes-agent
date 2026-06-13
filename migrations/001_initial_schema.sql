-- src initial schema
-- sessions mirrors hermes SessionDB (hermes_state.py SCHEMA_SQL) with
-- gateway-specific columns (workspace_id, feature_id, last_active_at, metadata).

CREATE TABLE IF NOT EXISTS sessions (
    id                  TEXT PRIMARY KEY,
    source              TEXT NOT NULL,
    user_id             TEXT,
    workspace_id        TEXT NOT NULL DEFAULT '',
    feature_id          TEXT NOT NULL DEFAULT '',
    model               TEXT,
    model_config        TEXT,
    system_prompt       TEXT,
    parent_session_id   TEXT REFERENCES sessions(id),
    started_at          DOUBLE PRECISION NOT NULL,
    ended_at            DOUBLE PRECISION,
    end_reason          TEXT,
    last_active_at      DOUBLE PRECISION NOT NULL,
    is_active           BOOLEAN NOT NULL DEFAULT TRUE,
    message_count       INTEGER NOT NULL DEFAULT 0,
    tool_call_count     INTEGER NOT NULL DEFAULT 0,
    input_tokens        INTEGER NOT NULL DEFAULT 0,
    output_tokens       INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens   INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens  INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens    INTEGER NOT NULL DEFAULT 0,
    api_call_count      INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd  DOUBLE PRECISION,
    actual_cost_usd     DOUBLE PRECISION,
    cost_status         TEXT,
    cost_source         TEXT,
    pricing_version     TEXT,
    billing_provider    TEXT,
    billing_base_url    TEXT,
    billing_mode        TEXT,
    cwd                 TEXT,
    title               TEXT,
    archived            BOOLEAN NOT NULL DEFAULT FALSE,
    metadata            JSONB NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_sessions_source  ON sessions(source);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);

CREATE TABLE IF NOT EXISTS messages (
    id                      BIGSERIAL PRIMARY KEY,
    session_id              TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role                    TEXT NOT NULL,
    content                 TEXT,
    tool_call_id            TEXT,
    tool_calls              TEXT,
    tool_name               TEXT,
    finish_reason           TEXT,
    reasoning               TEXT,
    reasoning_content       TEXT,
    reasoning_details       TEXT,
    codex_reasoning_items   TEXT,
    codex_message_items     TEXT,
    token_count             INTEGER,
    platform_message_id     TEXT,
    observed                BOOLEAN NOT NULL DEFAULT FALSE,
    active                  BOOLEAN NOT NULL DEFAULT TRUE,
    created_at              DOUBLE PRECISION NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_messages_session        ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_messages_session_active ON messages(session_id, active, created_at);
