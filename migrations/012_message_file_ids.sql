-- chat-file-upload: persist file attachments alongside image attachments.
-- Mirrors the image_ids pattern — same JSONB column type, same NOT NULL
-- DEFAULT '[]'::jsonb, same migration shape. File IDs are storage-service
-- UUIDs, resolved to fetchable URLs by the reading router.

BEGIN;

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS file_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;
