"""Tests for DM store functions and /dms API routes (agent-general-chat T1).

Test plan coverage:
  - create_dm: idempotent create — same pair twice returns same session id
  - create_dm: pair uniqueness is workspace-scoped (different workspaces → different sessions)
  - list_dms: scoped to caller — non-member sees no DMs
  - list_dms: returns caller's own DMs
  - POST /dms → 201 with session_id
  - POST /dms idempotent → 201 same session_id
  - POST /dms self-DM → 400
  - POST /dms missing workspace_id → 422
  - POST /dms missing other_member_id → 422
  - GET /dms → 200 with dms list
  - GET /dms missing workspace_id → 422
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, List
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


def _scalar_result(value):
    """Return a mock execute result whose scalar_one_or_none() returns value."""
    r = MagicMock()
    r.scalar_one_or_none.return_value = value
    return r


def _rows_result(rows: List[Any]):
    """Return a mock execute result whose all() returns rows."""
    r = MagicMock()
    r.all.return_value = rows
    return r


def _make_dm_row(
    id: str = "sess_dm1",
    title: str = None,
    feature_id: str = "",
    started_at: float = 1000.0,
    last_active_at: float = 1001.0,
    model: str = "claude-3",
    kind: str = "dm",
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
async def test_create_dm_returns_existing_if_found():
    """create_dm returns the existing session id when the pair already has a DM."""
    from src.db.store import create_dm

    db = _mock_db()
    db.execute = AsyncMock(return_value=_scalar_result("sess_existing_dm"))

    result = await create_dm(
        db, workspace_id="ws_1", member_a="user_a", member_b="user_b"
    )

    assert result == "sess_existing_dm"
    db.add.assert_not_called()
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_create_dm_creates_new_session_when_none_exists():
    """create_dm creates a new kind='dm', feature_id='' session when no existing DM is found."""
    from src.db.store import create_dm
    from src.db.models import Session, SessionMember

    db = _mock_db()
    captured_adds: List[Any] = []
    db.add = MagicMock(side_effect=lambda obj: captured_adds.append(obj))

    # First call: no existing DM; second call would be for members (no execute call)
    db.execute = AsyncMock(return_value=_scalar_result(None))

    async def _flush():
        sessions = [o for o in captured_adds if isinstance(o, Session)]
        if sessions:
            sessions[0].id = "sess_new_dm"

    db.flush = AsyncMock(side_effect=_flush)

    result = await create_dm(
        db, workspace_id="ws_1", member_a="user_a", member_b="user_b"
    )

    sessions = [o for o in captured_adds if isinstance(o, Session)]
    members = [o for o in captured_adds if isinstance(o, SessionMember)]

    assert result == "sess_new_dm"
    assert len(sessions) == 1
    assert sessions[0].kind == "dm"
    assert sessions[0].feature_id == ""
    assert sessions[0].workspace_id == "ws_1"

    # Both members added
    assert len(members) == 2
    member_ids = {m.user_id for m in members}
    assert member_ids == {"user_a", "user_b"}

    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_create_dm_idempotent_same_pair_same_workspace():
    """create_dm called twice with same pair returns same session id (idempotent).

    First call creates a new session (execute returns None); second call finds the
    existing session via execute and returns its id without creating a new row.
    """
    from src.db.store import create_dm
    from src.db.models import Session

    db = _mock_db()

    created_ids: List[str] = []
    captured_adds: List[Any] = []
    db.add = MagicMock(side_effect=lambda obj: captured_adds.append(obj))

    call_count = 0

    async def _execute_side(query):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _scalar_result(None)  # first call: no existing DM
        # second call: return the id the first call created
        return _scalar_result(created_ids[0] if created_ids else "sess_dm_abc")

    db.execute = AsyncMock(side_effect=_execute_side)

    async def _flush():
        sessions = [o for o in captured_adds if isinstance(o, Session)]
        if sessions:
            created_ids.append(sessions[0].id)

    db.flush = AsyncMock(side_effect=_flush)

    result1 = await create_dm(
        db, workspace_id="ws_1", member_a="user_a", member_b="user_b"
    )
    result2 = await create_dm(
        db, workspace_id="ws_1", member_a="user_a", member_b="user_b"
    )

    # Both calls return the same session id — idempotent
    assert result1 == result2
    # First call created a new session; second call found it
    assert len(created_ids) == 1
    db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_create_dm_pair_unique_per_workspace():
    """create_dm creates separate sessions for the same pair in different workspaces."""
    from src.db.store import create_dm
    from src.db.models import Session

    db_ws1 = _mock_db()
    captured_ws1: List[Any] = []
    db_ws1.add = MagicMock(side_effect=lambda obj: captured_ws1.append(obj))
    db_ws1.execute = AsyncMock(return_value=_scalar_result(None))

    async def _flush_ws1():
        sessions = [o for o in captured_ws1 if isinstance(o, Session)]
        if sessions:
            sessions[0].id = "sess_ws1_dm"

    db_ws1.flush = AsyncMock(side_effect=_flush_ws1)

    db_ws2 = _mock_db()
    captured_ws2: List[Any] = []
    db_ws2.add = MagicMock(side_effect=lambda obj: captured_ws2.append(obj))
    db_ws2.execute = AsyncMock(return_value=_scalar_result(None))

    async def _flush_ws2():
        sessions = [o for o in captured_ws2 if isinstance(o, Session)]
        if sessions:
            sessions[0].id = "sess_ws2_dm"

    db_ws2.flush = AsyncMock(side_effect=_flush_ws2)

    id_ws1 = await create_dm(
        db_ws1, workspace_id="ws_1", member_a="user_a", member_b="user_b"
    )
    id_ws2 = await create_dm(
        db_ws2, workspace_id="ws_2", member_a="user_a", member_b="user_b"
    )

    assert id_ws1 == "sess_ws1_dm"
    assert id_ws2 == "sess_ws2_dm"
    assert id_ws1 != id_ws2


@pytest.mark.asyncio
async def test_list_dms_returns_caller_dms():
    """list_dms returns DM sessions the caller is a member of, with other_member_id resolved."""
    from src.db.store import list_dms

    db = _mock_db()
    row = _make_dm_row(id="sess_dm1")

    def _member_rows_result(pairs):
        r = MagicMock()
        r.all.return_value = pairs
        return r

    db.execute = AsyncMock(
        side_effect=[
            _rows_result([row]),
            _member_rows_result([("sess_dm1", "user_b")]),
        ]
    )

    dms = await list_dms(db, workspace_id="ws_1", user_id="user_a")

    assert len(dms) == 1
    assert dms[0]["id"] == "sess_dm1"
    assert dms[0]["kind"] == "dm"
    assert dms[0]["feature_id"] == ""
    assert dms[0]["other_member_id"] == "user_b"


@pytest.mark.asyncio
async def test_list_dms_scoped_to_caller():
    """list_dms returns empty list when caller has no DMs."""
    from src.db.store import list_dms

    db = _mock_db()
    db.execute = AsyncMock(return_value=_rows_result([]))

    dms = await list_dms(db, workspace_id="ws_1", user_id="user_no_dms")

    assert dms == []


# ---------------------------------------------------------------------------
# DMs router unit tests (no Postgres — mock store functions)
# ---------------------------------------------------------------------------


def _make_dms_app():
    """Build a minimal FastAPI test app with the dms router."""
    from fastapi import FastAPI
    from src.api.routers.dms import router
    from src.api.deps import get_db
    from src.api.identity import require_identity, Identity

    async def _override_db():
        yield _mock_db()

    def _override_identity():
        return Identity(user_id="user_caller", org_id="org_1")

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.dependency_overrides[get_db] = _override_db
    app.dependency_overrides[require_identity] = _override_identity
    return app


@pytest.mark.asyncio
async def test_post_dms_returns_201_with_session_id():
    """POST /dms → 201 with session_id."""
    with (
        patch(
            "src.api.routers.dms.create_dm",
            new=AsyncMock(return_value="sess_dm_new"),
        ),
        patch(
            "src.api.routers.dms.list_org_members",
            new=AsyncMock(return_value={}),
        ),
    ):
        from fastapi.testclient import TestClient

        app = _make_dms_app()
        client = TestClient(app)
        resp = client.post(
            "/api/v1/dms",
            json={"workspace_id": "ws_1", "other_member_id": "user_b"},
        )
    assert resp.status_code == 201
    assert resp.json()["session_id"] == "sess_dm_new"


@pytest.mark.asyncio
async def test_post_dms_idempotent_returns_existing_session():
    """POST /dms called twice returns the same session_id."""
    with (
        patch(
            "src.api.routers.dms.create_dm",
            new=AsyncMock(return_value="sess_dm_existing"),
        ),
        patch(
            "src.api.routers.dms.list_org_members",
            new=AsyncMock(return_value={}),
        ),
    ):
        from fastapi.testclient import TestClient

        app = _make_dms_app()
        client = TestClient(app)

        resp1 = client.post(
            "/api/v1/dms",
            json={"workspace_id": "ws_1", "other_member_id": "user_b"},
        )
        resp2 = client.post(
            "/api/v1/dms",
            json={"workspace_id": "ws_1", "other_member_id": "user_b"},
        )
    assert resp1.status_code == 201
    assert resp2.status_code == 201
    assert resp1.json()["session_id"] == resp2.json()["session_id"]


@pytest.mark.asyncio
async def test_post_dms_self_dm_returns_400():
    """POST /dms with other_member_id == caller → 400."""
    with patch(
        "src.api.routers.dms.list_org_members",
        new=AsyncMock(return_value={}),
    ):
        from fastapi.testclient import TestClient

        app = _make_dms_app()
        client = TestClient(app)
        resp = client.post(
            "/api/v1/dms",
            json={"workspace_id": "ws_1", "other_member_id": "user_caller"},
        )
    assert resp.status_code == 400
    assert "yourself" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_post_dms_missing_workspace_id_422():
    """POST /dms without workspace_id → 422."""
    from fastapi.testclient import TestClient

    app = _make_dms_app()
    client = TestClient(app)
    resp = client.post("/api/v1/dms", json={"other_member_id": "user_b"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_dms_missing_other_member_id_422():
    """POST /dms without other_member_id → 422."""
    from fastapi.testclient import TestClient

    app = _make_dms_app()
    client = TestClient(app)
    resp = client.post("/api/v1/dms", json={"workspace_id": "ws_1"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_post_dms_nonmember_returns_404():
    """POST /dms where other_member_id not in the caller's org → 404."""
    with (
        patch(
            "src.api.routers.dms.get_workspace_organization_id",
            new=AsyncMock(return_value="org_1"),
        ),
        patch(
            "src.api.routers.dms.list_org_members",
            new=AsyncMock(return_value={"user_x": {}, "user_y": {}}),
        ),
    ):
        from fastapi.testclient import TestClient

        app = _make_dms_app()
        client = TestClient(app)
        resp = client.post(
            "/api/v1/dms",
            json={
                "workspace_id": "ws_1",
                "other_member_id": "user_not_a_member",
            },
        )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_dms_returns_200_with_list():
    """GET /dms returns list of caller's DMs."""
    dms_data = [
        {
            "id": "sess_dm1",
            "title": None,
            "feature_id": "",
            "started_at": 1000.0,
            "last_active_at": 1001.0,
            "model": "claude-3",
            "kind": "dm",
        }
    ]
    with patch(
        "src.api.routers.dms.list_dms",
        new=AsyncMock(return_value=dms_data),
    ):
        from fastapi.testclient import TestClient

        app = _make_dms_app()
        client = TestClient(app)
        resp = client.get("/api/v1/dms?workspace_id=ws_1")
    assert resp.status_code == 200
    assert resp.json()["dms"] == dms_data


@pytest.mark.asyncio
async def test_get_dms_enriches_other_member_name():
    """GET /dms attaches other_member_name/avatar resolved by other_member_id."""
    dms_data = [
        {
            "id": "sess_dm1",
            "title": None,
            "feature_id": "",
            "started_at": 1000.0,
            "last_active_at": 1001.0,
            "model": "claude-3",
            "kind": "dm",
            "other_member_id": "user_b",
        }
    ]
    with (
        patch("src.api.routers.dms.list_dms", new=AsyncMock(return_value=dms_data)),
        patch(
            "src.api.routers.dms.list_users_by_ids",
            new=AsyncMock(return_value={"user_b": {"display_name": "Bob", "avatar_url": "https://x/b.png"}}),
        ),
    ):
        from fastapi.testclient import TestClient

        app = _make_dms_app()
        client = TestClient(app)
        resp = client.get("/api/v1/dms?workspace_id=ws_1")
    assert resp.status_code == 200
    dm = resp.json()["dms"][0]
    assert dm["other_member_name"] == "Bob"
    assert dm["other_member_avatar_url"] == "https://x/b.png"


@pytest.mark.asyncio
async def test_get_dms_missing_workspace_id_422():
    """GET /dms without workspace_id → 422."""
    from fastapi.testclient import TestClient

    app = _make_dms_app()
    client = TestClient(app)
    resp = client.get("/api/v1/dms")
    assert resp.status_code == 422
