-- Convert session ids from the legacy "sess_<hex>" text format to native UUID,
-- matching user-service's UUID-typed turn_cost.session_id (so cost events no
-- longer need an id-mapping shim) and giving the column DB-level validation.
--
-- All existing sessions are cleared first (agreed: no session data needs
-- preserving) so the column type can change cleanly and no legacy non-UUID ids
-- remain. New ids are generated as UUIDs by _new_session_id() in
-- src/db/store.py and src/db/store_v4.py.
--
-- NOTE: this migration is intentionally destructive and one-shot (the runner in
-- store.py records it in schema_migrations so it never re-runs).

TRUNCATE TABLE sessions CASCADE;

-- Drop FKs referencing sessions(id) so the PK / FK column types can change.
ALTER TABLE messages         DROP CONSTRAINT IF EXISTS messages_session_id_fkey;
ALTER TABLE session_members  DROP CONSTRAINT IF EXISTS session_members_session_id_fkey;
ALTER TABLE message_mentions DROP CONSTRAINT IF EXISTS message_mentions_session_id_fkey;
ALTER TABLE sessions         DROP CONSTRAINT IF EXISTS sessions_parent_session_id_fkey;

-- Convert id columns to UUID (tables are empty, so USING is a no-op on data).
ALTER TABLE sessions         ALTER COLUMN id                TYPE UUID USING id::uuid;
ALTER TABLE sessions         ALTER COLUMN parent_session_id TYPE UUID USING parent_session_id::uuid;
ALTER TABLE messages         ALTER COLUMN session_id        TYPE UUID USING session_id::uuid;
ALTER TABLE session_members  ALTER COLUMN session_id        TYPE UUID USING session_id::uuid;
ALTER TABLE message_mentions ALTER COLUMN session_id        TYPE UUID USING session_id::uuid;

-- Recreate the FKs (same names + cascade behavior as the original inline defs).
ALTER TABLE sessions ADD CONSTRAINT sessions_parent_session_id_fkey
    FOREIGN KEY (parent_session_id) REFERENCES sessions(id);
ALTER TABLE messages ADD CONSTRAINT messages_session_id_fkey
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE;
ALTER TABLE session_members ADD CONSTRAINT session_members_session_id_fkey
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE;
ALTER TABLE message_mentions ADD CONSTRAINT message_mentions_session_id_fkey
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE;
