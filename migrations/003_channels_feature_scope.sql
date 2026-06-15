-- m3-agent-chat: make channels feature-scoped.
-- Channels now belong to a (workspace, feature) pair rather than a whole
-- workspace, so the unique channel-name constraint is scoped per feature: the
-- same channel name may exist under different features of a workspace.

DROP INDEX IF EXISTS idx_sessions_channel_name_unique;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_channel_name_unique
    ON sessions (workspace_id, feature_id, lower(title))
    WHERE kind = 'channel' AND archived = FALSE;
