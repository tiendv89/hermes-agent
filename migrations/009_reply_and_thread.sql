-- chat-reply-and-thread: additive migration
-- Adds message-level reply/thread linkage to the messages table.
-- Both columns are nullable so existing rows and every existing code path are
-- unaffected (NULL = no reply/thread linkage, same as author_id's v4 rollout).

BEGIN;

-- reply_to_message_id: the specific message this message is visually replying
-- to (G1). Set on direct inline replies *and* on replies inside a thread.
-- NULL means this is a plain (non-reply) message.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS reply_to_message_id BIGINT
        REFERENCES messages(id);

-- thread_root_id: the root message of the message thread this message belongs
-- to (G2). NULL = lives in the main transcript. Non-NULL = scoped to the
-- thread rooted at that message id, excluded from the main transcript.
ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS thread_root_id BIGINT
        REFERENCES messages(id);

-- Composite index for fetching a thread's replies in order (most-used query).
CREATE INDEX IF NOT EXISTS idx_messages_thread_root
    ON messages (session_id, thread_root_id, created_at);

-- Index for quoted-parent lookups (render the preview strip for a reply).
CREATE INDEX IF NOT EXISTS idx_messages_reply_to
    ON messages (reply_to_message_id);

COMMIT;
