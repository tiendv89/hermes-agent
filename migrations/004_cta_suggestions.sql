-- m3-agent-cta: add cta_suggestions column to messages.
-- Additive migration: new column defaults to '[]'::jsonb so existing rows
-- are unaffected and the change is zero-downtime.

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS cta_suggestions JSONB NOT NULL DEFAULT '[]'::jsonb;
