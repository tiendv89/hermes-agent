"""Tests for the workspace-level team thread API and store (T9).

Covers the T9 test plan from tasks.md:
  - POST /threads → kind='thread', feature_id='', creator auto-joined
  - POST /threads with members → extra members also added
  - GET /threads → returns own ∪ member-of workspace threads for caller
  - GET /threads → non-members excluded (member-scoped listing)
  - list_workspace_threads excludes feature threads (feature_id != '')
  - list_member_sessions includes workspace threads (no feature_id filter)
  - Missing identity → 400
  - Missing workspace_id → 400
  - create_workspace_thread store unit: correct Session row + members
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_db():
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock()
    db.delete = AsyncMock()
    return db


def _make_thread_row(
    id: str = "sess_t1",
    title: str = "Team Chat",
    feature_id: str = "",
    started_at: float = 1000.0,
    last_active_at: float = 1001.0,
    model: str = "claude-3",
    kind: str = "thread",
):
    row = MagicMock()
    row.id = id
    row.title = title
    row.feature_id = feature_id
    row.started_at = started_at
    row.last_active_at = last_active_at
    row.model = model
    row.kind = kind
    return row


# ---------------------------------------------------------------------------
# Store unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_workspace_thread_creates_session_and_joins_creator():
    """create_workspace_thread creates a kind='thread', feature_id='' Session and auto-joins creator."""
    from src.db.store import create_workspace_thread

    db = _mock_db()
    captured_adds: list[Any] = []
    db.add = MagicMock(side_effect=lambda obj: captured_adds.append(obj))

    # Simulate flush populating session.id
    async def _flush():
        if captured_adds:
            captured_adds[0].id = "sess_ws_123"

    db.flush = AsyncMock(side_effect=_flush)

    await create_workspace_thread(
        db,
        workspace_id="ws_1",
        creator_user_id="user_creator",
    )

    # Session row
    from src.db.models import Session, SessionMember

    sessions = [o for o in captured_adds if isinstance(o, Session)]
    members = [o for o in captured_adds if isinstance(o, SessionMember)]

    assert len(sessions) == 1
    sess = sessions[0]
    assert sess.kind == "thread"
    assert sess.feature_id == ""
    assert sess.workspace_id == "ws_1"
    assert sess.user_id == "user_creator"

    # Creator auto-joined
    assert len(members) == 1
    assert members[0].user_id == "user_creator"
    assert members[0].session_id == "sess_ws_123"

    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_create_workspace_thread_adds_initial_members():
    """create_workspace_thread adds extra members from the members list (skips creator duplicate)."""
    from src.db.store import create_workspace_thread

    db = _mock_db()
    captured_adds: list[Any] = []
    db.add = MagicMock(side_effect=lambda obj: captured_adds.append(obj))

    async def _flush():
        if captured_adds:
            captured_adds[0].id = "sess_ws_456"

    db.flush = AsyncMock(side_effect=_flush)

    await create_workspace_thread(
        db,
        workspace_id="ws_1",
        creator_user_id="user_a",
        members=["user_a", "user_b", "user_c"],  # user_a is creator — should be skipped
    )

    from src.db.models import SessionMember

    members = [o for o in captured_adds if isinstance(o, SessionMember)]
    user_ids = {m.user_id for m in members}
    # creator + user_b + user_c; user_a not duplicated
    assert user_ids == {"user_a", "user_b", "user_c"}
    assert len(members) == 3


@pytest.mark.asyncio
async def test_create_workspace_thread_optional_title():
    """create_workspace_thread stores title when provided, None when omitted."""
    from src.db.models import Session
    from src.db.store import create_workspace_thread

    db = _mock_db()
    captured_adds: list[Any] = []
    db.add = MagicMock(side_effect=lambda obj: captured_adds.append(obj))

    async def _flush():
        if captured_adds:
            captured_adds[0].id = "sess_ws_789"

    db.flush = AsyncMock(side_effect=_flush)

    await create_workspace_thread(db, workspace_id="ws_1", creator_user_id="u1", title="Q1 Planning")

    session = next(o for o in captured_adds if isinstance(o, Session))
    assert session.title == "Q1 Planning"


@pytest.mark.asyncio
async def test_list_workspace_threads_returns_own_and_member_of():
    """list_workspace_threads returns threads the user owns or is a member of."""
    from src.db.store import list_workspace_threads

    db = _mock_db()
    row = _make_thread_row(id="sess_t1", feature_id="")
    result_mock = MagicMock()
    result_mock.all.return_value = [row]
    db.execute = AsyncMock(return_value=result_mock)

    threads = await list_workspace_threads(db, workspace_id="ws_1", user_id="user_a")

    assert len(threads) == 1
    assert threads[0]["id"] == "sess_t1"
    assert threads[0]["feature_id"] == ""
    assert threads[0]["kind"] == "thread"


@pytest.mark.asyncio
async def test_list_workspace_threads_untitled_fallback():
    """list_workspace_threads uses '(untitled)' when title is None."""
    from src.db.store import list_workspace_threads

    db = _mock_db()
    row = _make_thread_row(id="sess_t2", title=None, feature_id="")
    result_mock = MagicMock()
    result_mock.all.return_value = [row]
    db.execute = AsyncMock(return_value=result_mock)

    threads = await list_workspace_threads(db, workspace_id="ws_1", user_id="user_a")
    assert threads[0]["title"] == "(untitled)"


@pytest.mark.asyncio
async def test_list_workspace_threads_scopes_to_hermes_agent_source():
    """An IDE coding session (source='coding-ide') has the same kind='thread'/
    feature_id='' shape as a genuine web workspace thread (the VS Code
    extension's POST /session never sets a feature_id, and every session
    defaults to kind='thread') — without an explicit source filter it would
    leak into the browser's workspace-threads sidebar as an empty,
    unopenable "thread". Asserts the query Session.execute() actually
    receives includes a source='hermes-agent' predicate, not just that the
    (mocked, source-blind) row mapping still works."""
    from src.db.store import list_workspace_threads

    db = _mock_db()
    result_mock = MagicMock()
    result_mock.all.return_value = []
    db.execute = AsyncMock(return_value=result_mock)

    await list_workspace_threads(db, workspace_id="ws_1", user_id="user_a")

    statement = db.execute.call_args[0][0]
    compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
    assert "source" in compiled
    assert "hermes-agent" in compiled


@pytest.mark.asyncio
async def test_list_member_sessions_includes_workspace_threads():
    """list_member_sessions returns both feature threads and workspace threads (no feature_id filter)."""
    from src.db.store import list_member_sessions

    db = _mock_db()
    # One feature thread and one workspace thread
    row_feature = _make_thread_row(id="sess_feature", feature_id="feat_1")
    row_workspace = _make_thread_row(id="sess_ws", feature_id="")
    result_mock = MagicMock()
    result_mock.all.return_value = [row_feature, row_workspace]
    db.execute = AsyncMock(return_value=result_mock)

    sessions = await list_member_sessions(db, workspace_id="ws_1", user_id="user_a")
    ids = [s["id"] for s in sessions]
    assert "sess_feature" in ids
    assert "sess_ws" in ids


# ---------------------------------------------------------------------------
# Threads router unit tests (no Postgres — mock store functions)
# ---------------------------------------------------------------------------


def _make_app():
    """Build a minimal FastAPI test app with the threads router."""
    from fastapi import FastAPI

    from src.api.deps import get_db
    from src.api.identity import Identity, require_identity
    from src.api.routers.threads import router

    async def _override_db():
        yield _mock_db()

    def _override_identity():
        return Identity(user_id="user_test", org_id="org_1")

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = _override_identity
    return app


@pytest.mark.asyncio
async def test_create_thread_success_201():
    """POST /threads → 201 with thread_id."""
    with patch(
        "src.api.routers.threads.create_workspace_thread",
        new=AsyncMock(return_value="sess_new_thread"),
    ):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/v1/threads",
            json={"workspace_id": "ws_1", "title": "Planning"},
        )
    assert resp.status_code == 201
    assert resp.json()["thread_id"] == "sess_new_thread"


@pytest.mark.asyncio
async def test_create_thread_with_members():
    """POST /threads with members list passes them to store."""
    captured: dict[str, Any] = {}

    async def _mock_create(db, workspace_id, creator_user_id, title, members):
        captured["members"] = members
        return "sess_abc"

    with patch("src.api.routers.threads.create_workspace_thread", new=_mock_create):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)
        resp = client.post(
            "/api/v1/threads",
            json={"workspace_id": "ws_1", "members": ["user_b", "user_c"]},
        )
    assert resp.status_code == 201
    assert set(captured["members"]) == {"user_b", "user_c"}


@pytest.mark.asyncio
async def test_create_thread_missing_workspace_id_422():
    """POST /threads without workspace_id → 422 (Pydantic required-field validation)."""
    from fastapi.testclient import TestClient

    app = _make_app()
    client = TestClient(app)
    resp = client.post("/api/v1/threads", json={"title": "No workspace"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_create_thread_empty_workspace_id_400():
    """POST /threads with workspace_id='' → 400 (router validation)."""
    from fastapi.testclient import TestClient

    app = _make_app()
    client = TestClient(app)
    resp = client.post("/api/v1/threads", json={"workspace_id": "", "title": "No workspace"})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_list_threads_returns_threads():
    """GET /threads returns threads list from store."""
    threads_data = [
        {
            "id": "sess_t1",
            "title": "Team Chat",
            "feature_id": "",
            "started_at": 1000.0,
            "last_active_at": 1001.0,
            "model": "claude-3",
            "kind": "thread",
        }
    ]
    with patch(
        "src.api.routers.threads.list_workspace_threads",
        new=AsyncMock(return_value=threads_data),
    ):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/v1/threads?workspace_id=ws_1")
    assert resp.status_code == 200
    assert resp.json()["threads"] == threads_data


@pytest.mark.asyncio
async def test_list_threads_excludes_non_members():
    """GET /threads returns empty list when caller is not a member of any thread."""
    with patch(
        "src.api.routers.threads.list_workspace_threads",
        new=AsyncMock(return_value=[]),
    ):
        from fastapi.testclient import TestClient

        app = _make_app()
        client = TestClient(app)
        resp = client.get("/api/v1/threads?workspace_id=ws_2")
    assert resp.status_code == 200
    assert resp.json()["threads"] == []


@pytest.mark.asyncio
async def test_list_threads_missing_workspace_id_422():
    """GET /threads without workspace_id → 422 (FastAPI validation)."""
    from fastapi.testclient import TestClient

    app = _make_app()
    client = TestClient(app)
    resp = client.get("/api/v1/threads")
    assert resp.status_code == 422
