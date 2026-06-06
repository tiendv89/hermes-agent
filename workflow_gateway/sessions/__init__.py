"""Postgres-backed session and message store for the workflow gateway.

Schema mirrors swell-hermes (voyager_sessions_v4 / voyager_messages_v4) so
the same frontend client and SSE envelope work unchanged.

Tables are created on first startup via ``init_db(pool)``. The gateway owns
these tables; hermes-agent is given ``session_db=NoOpSessionDB()`` so it does
not write to its own SQLite.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional

import asyncpg

# ---------------------------------------------------------------------------
# Schema DDL
# ---------------------------------------------------------------------------

_CREATE_SESSIONS_TABLE = """
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
"""

_CREATE_MESSAGES_TABLE = """
CREATE TABLE IF NOT EXISTS voyager_messages_v4 (
    id          BIGSERIAL PRIMARY KEY,
    session_id  TEXT NOT NULL REFERENCES voyager_sessions_v4(session_id) ON DELETE CASCADE,
    role        TEXT NOT NULL,
    content     TEXT NOT NULL,
    created_at  DOUBLE PRECISION NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_vm4_session ON voyager_messages_v4(session_id, created_at);
"""


async def init_db(pool: asyncpg.Pool) -> None:
    """Create tables if they do not exist."""
    async with pool.acquire() as conn:
        await conn.execute(_CREATE_SESSIONS_TABLE)
        await conn.execute(_CREATE_MESSAGES_TABLE)


# ---------------------------------------------------------------------------
# Session CRUD
# ---------------------------------------------------------------------------

def _new_session_id() -> str:
    return "sess_" + secrets.token_hex(16)


async def create_session(
    pool: asyncpg.Pool,
    user_id: str,
    workspace_id: str = "",
    feature_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> str:
    """Insert a new session row and return the session_id."""
    session_id = _new_session_id()
    now = time.time()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO voyager_sessions_v4
                (session_id, user_id, workspace_id, feature_id, created_at, last_active_at, metadata)
            VALUES ($1, $2, $3, $4, $5, $5, $6)
            """,
            session_id,
            user_id,
            workspace_id,
            feature_id,
            now,
            json.dumps(metadata or {}),
        )
    return session_id


async def get_session(
    pool: asyncpg.Pool,
    session_id: str,
) -> Optional[Dict[str, Any]]:
    """Return the session row as a dict, or None if not found."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM voyager_sessions_v4 WHERE session_id = $1",
            session_id,
        )
    if row is None:
        return None
    return dict(row)


async def touch_session(pool: asyncpg.Pool, session_id: str) -> None:
    """Update last_active_at to now."""
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE voyager_sessions_v4 SET last_active_at = $1 WHERE session_id = $2",
            time.time(),
            session_id,
        )


# ---------------------------------------------------------------------------
# Message CRUD
# ---------------------------------------------------------------------------

async def append_message(
    pool: asyncpg.Pool,
    session_id: str,
    role: str,
    content: str,
    metadata: Optional[Dict[str, Any]] = None,
) -> int:
    """Append a message and return its id."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO voyager_messages_v4 (session_id, role, content, created_at, metadata)
            VALUES ($1, $2, $3, $4, $5)
            RETURNING id
            """,
            session_id,
            role,
            content,
            time.time(),
            json.dumps(metadata or {}),
        )
    return row["id"]


async def get_messages(
    pool: asyncpg.Pool,
    session_id: str,
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Return the most recent messages for a session, oldest first."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, role, content, created_at, metadata
            FROM voyager_messages_v4
            WHERE session_id = $1
            ORDER BY created_at ASC
            LIMIT $2
            """,
            session_id,
            limit,
        )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# NoOpSessionDB — passed to AIAgent so it skips its own SQLite writes
# ---------------------------------------------------------------------------

class NoOpSessionDB:
    """A do-nothing session database for AIAgent.

    The gateway owns Postgres session state. We pass this to AIAgent so it
    never touches its own SQLite (~/.hermes/state.db).
    """

    def create_session(self, *args: Any, **kwargs: Any) -> None:
        pass

    def append_message(self, *args: Any, **kwargs: Any) -> None:
        pass

    def get_messages(self, *args: Any, **kwargs: Any) -> List:
        return []

    def get_session(self, *args: Any, **kwargs: Any) -> Optional[Dict]:
        return None

    def update_session(self, *args: Any, **kwargs: Any) -> None:
        pass

    def delete_session(self, *args: Any, **kwargs: Any) -> None:
        pass
