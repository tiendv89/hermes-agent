-- model_catalog: admin-editable model identity. One row per model.
CREATE TABLE IF NOT EXISTS model_catalog
(
    model_id     TEXT PRIMARY KEY,
    display_name TEXT        NOT NULL,
    provider     TEXT        NOT NULL,
    is_active    BOOLEAN     NOT NULL DEFAULT TRUE,
    is_default   BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Enforce at most one default model at a time.
CREATE UNIQUE INDEX IF NOT EXISTS model_catalog_one_default ON model_catalog (is_default) WHERE is_default;

-- Backfill from today's hardcoded SUPPORTED_MODELS.
INSERT INTO model_catalog (model_id, display_name, provider, is_active, is_default) VALUES
  ('claude-opus-4-8',   'Claude Opus 4.8',   'anthropic', TRUE, FALSE),
  ('claude-sonnet-4-6', 'Claude Sonnet 4.6', 'anthropic', TRUE, TRUE),
  ('claude-haiku-4-5',  'Claude Haiku 4.5',  'anthropic', TRUE, FALSE),
  ('deepseek-v4-flash', 'DeepSeek V4 Flash', 'deepseek',  TRUE, FALSE),
  ('deepseek-v4-pro',   'DeepSeek V4 Pro',   'deepseek',  TRUE, FALSE)
ON CONFLICT (model_id) DO NOTHING
