-- agent-chat-images: persist images attached to a message.
-- Previously image_ids was an ephemeral request field only used to let the
-- agent download+view images server-side (see agent_dispatch.py) — never
-- stored, so attachments vanished on reload or for any other viewer. This
-- column makes the association durable, same pattern as cta_suggestions.

BEGIN;

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS image_ids JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMIT;
