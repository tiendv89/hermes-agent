"""Unit and integration tests for can_view_session + implicit join (T2).

Covers the T2 test plan:
  - can_view_session: non-member org user authorized on feature session
    (kind='thread', feature_id != '').
  - can_view_session: workspace Team Chat thread (feature_id='') rejects
    non-members even when caller_is_workspace_member=True.
  - can_view_session: non-org-member rejected on feature session
    (caller_is_workspace_member=False).
  - can_view_session: owner always authorized regardless of kind/feature_id.
  - is_org_member: permissive in dev mode (USER_SERVICE_URL unset).
  - is_org_member: delegates to get_org_role when USER_SERVICE_URL is set.
  - stream endpoint: authorized org member gets implicit session_members row.
  - stream endpoint: non-org-member rejected on feature session (403).
  - messages endpoint: authorized org member gets implicit join on first post.
  - messages endpoint: workspace thread non-member still rejected.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
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


def _make_session(
    session_id="sess_feat_1",
    workspace_id="ws_1",
    user_id="owner_user",
    kind="thread",
    feature_id="feat-abc",
):
    s = MagicMock()
    s.id = session_id
    s.workspace_id = workspace_id
    s.user_id = user_id
    s.kind = kind
    s.feature_id = feature_id
    s.archived = False
    return s


# ---------------------------------------------------------------------------
# can_view_session — store-layer unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_can_view_session_owner_always_authorized():
    """Session owner is authorized regardless of kind or feature_id."""
    from src.db.store import can_view_session

    db = _mock_db()
    session = _make_session(user_id="owner", kind="thread", feature_id="feat-1")

    result = await can_view_session(
        db, session, "owner", caller_is_workspace_member=False
    )

    assert result is True
    db.get.assert_not_called()  # no DB lookup needed for owner


@pytest.mark.asyncio
async def test_can_view_session_feature_thread_org_member_authorized():
    """Non-member org user is authorized to view a feature session."""
    from src.db.store import can_view_session

    db = _mock_db()
    session = _make_session(user_id="owner", kind="thread", feature_id="feat-1")

    result = await can_view_session(
        db, session, "other_user", caller_is_workspace_member=True
    )

    assert result is True
    db.get.assert_not_called()  # no DB lookup needed — policy allows


@pytest.mark.asyncio
async def test_can_view_session_feature_thread_non_org_member_rejected():
    """A caller who is NOT an org member is rejected on a feature session."""
    from src.db.store import can_view_session

    db = _mock_db()
    db.get = AsyncMock(return_value=None)  # no explicit session_members row either
    session = _make_session(user_id="owner", kind="thread", feature_id="feat-1")

    result = await can_view_session(
        db, session, "outsider_user", caller_is_workspace_member=False
    )

    assert result is False


@pytest.mark.asyncio
async def test_can_view_session_workspace_thread_non_member_rejected():
    """Workspace Team Chat thread (feature_id='') rejects non-members."""
    from src.db.store import can_view_session

    db = _mock_db()
    db.get = AsyncMock(return_value=None)  # not an explicit member
    session = _make_session(user_id="owner", kind="thread", feature_id="")

    result = await can_view_session(
        db, session, "non_member", caller_is_workspace_member=True
    )

    assert result is False
    db.get.assert_called_once()  # explicit membership check was made


@pytest.mark.asyncio
async def test_can_view_session_workspace_thread_explicit_member_authorized():
    """Workspace Team Chat thread allows an explicit session_members row."""
    from src.db.store import can_view_session
    from src.db.models import SessionMember

    existing_row = MagicMock(spec=SessionMember)
    db = _mock_db()
    db.get = AsyncMock(return_value=existing_row)
    session = _make_session(user_id="owner", kind="thread", feature_id="")

    result = await can_view_session(
        db, session, "explicit_member", caller_is_workspace_member=False
    )

    assert result is True


@pytest.mark.asyncio
async def test_can_view_session_channel_non_member_requires_explicit_join():
    """Channels retain existing behavior: non-member needs explicit session_members row."""
    from src.db.store import can_view_session

    db = _mock_db()
    db.get = AsyncMock(return_value=None)
    session = _make_session(user_id="owner", kind="channel", feature_id="feat-1")

    result = await can_view_session(
        db, session, "non_member", caller_is_workspace_member=True
    )

    assert result is False


# ---------------------------------------------------------------------------
# is_org_member — user_service_client unit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_is_org_member_permissive_without_user_service_url():
    """is_org_member returns True when USER_SERVICE_URL is unset (dev mode)."""
    from src.services.user_service_client import is_org_member

    with patch.dict("os.environ", {}, clear=False):
        import os

        os.environ.pop("USER_SERVICE_URL", None)
        result = await is_org_member("org-1", "user-a")

    assert result is True


@pytest.mark.asyncio
async def test_is_org_member_empty_org_id_returns_false():
    """is_org_member returns False when org_id is empty."""
    from src.services.user_service_client import is_org_member

    with patch.dict("os.environ", {"USER_SERVICE_URL": "http://user-service:8080"}):
        result = await is_org_member("", "user-a")

    assert result is False


@pytest.mark.asyncio
async def test_is_org_member_delegates_to_get_org_role_member():
    """is_org_member returns True when get_org_role returns a role."""
    from src.services.user_service_client import is_org_member

    with (
        patch.dict("os.environ", {"USER_SERVICE_URL": "http://user-service:8080"}),
        patch(
            "src.services.user_service_client.get_org_role",
            new=AsyncMock(return_value="member"),
        ),
    ):
        result = await is_org_member("org-1", "user-a")

    assert result is True


@pytest.mark.asyncio
async def test_is_org_member_delegates_to_get_org_role_not_member():
    """is_org_member returns False when get_org_role returns None (not a member)."""
    from src.services.user_service_client import is_org_member

    with (
        patch.dict("os.environ", {"USER_SERVICE_URL": "http://user-service:8080"}),
        patch(
            "src.services.user_service_client.get_org_role",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await is_org_member("org-1", "outsider")

    assert result is False


@pytest.mark.asyncio
async def test_is_org_member_exception_returns_false():
    """is_org_member returns False when user-service raises an unexpected error."""
    from src.services.user_service_client import is_org_member

    with (
        patch.dict("os.environ", {"USER_SERVICE_URL": "http://user-service:8080"}),
        patch(
            "src.services.user_service_client.get_org_role",
            new=AsyncMock(side_effect=Exception("connection refused")),
        ),
    ):
        result = await is_org_member("org-1", "user-a")

    assert result is False


# ---------------------------------------------------------------------------
# stream endpoint — router-level integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_feature_session_org_member_authorized_and_implicit_join():
    """Org member authorized on a feature session → add_member called (implicit join).

    Tests the stream handler's auth+join logic directly — avoids consuming the
    infinite SSE stream by calling the route function with mocked dependencies
    rather than through a full TestClient.
    """
    from src.api.routers.stream import stream_thread

    feature_session = _make_session(
        session_id="sess_feat_1",
        user_id="other_owner",
        kind="thread",
        feature_id="feat-abc",
    )
    add_member_mock = AsyncMock()
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "non_member_user"
    identity.org_id = "org-1"

    fake_bus = MagicMock()
    fake_bus.subscribe_raw = MagicMock()
    fake_bus.unsubscribe_raw = MagicMock()

    with (
        patch(
            "src.api.routers.stream.get_session",
            new=AsyncMock(return_value=feature_session),
        ),
        patch(
            "src.api.routers.stream.get_workspace_organization_id",
            new=AsyncMock(return_value="org-1"),
        ),
        patch("src.api.routers.stream.is_org_member", new=AsyncMock(return_value=True)),
        patch(
            "src.api.routers.stream.can_view_session", new=AsyncMock(return_value=True)
        ),
        patch("src.api.routers.stream.add_member", new=add_member_mock),
        patch(
            "src.api.routers.stream.get_messages_since", new=AsyncMock(return_value=[])
        ),
        patch("src.api.routers.stream.attach_authors", new=AsyncMock()),
        patch("src.api.routers.stream.get_bus", return_value=fake_bus),
    ):
        # StreamingResponse is returned — check it's not a 403 and add_member was called.
        from starlette.responses import StreamingResponse

        resp = await stream_thread(
            session_id="sess_feat_1",
            since=None,
            identity=identity,
            db=db,
        )
        assert isinstance(resp, StreamingResponse)
        add_member_mock.assert_awaited_once_with(
            db, "sess_feat_1", "non_member_user", added_by="non_member_user"
        )


@pytest.mark.asyncio
async def test_stream_feature_session_non_org_member_rejected():
    """Non-org-member calling the stream handler → HTTPException 403."""
    from fastapi import HTTPException
    from src.api.routers.stream import stream_thread

    feature_session = _make_session(
        session_id="sess_feat_2",
        user_id="other_owner",
        kind="thread",
        feature_id="feat-abc",
    )
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "outsider"
    identity.org_id = "org-1"

    with (
        patch(
            "src.api.routers.stream.get_session",
            new=AsyncMock(return_value=feature_session),
        ),
        patch(
            "src.api.routers.stream.get_workspace_organization_id",
            new=AsyncMock(return_value="org-1"),
        ),
        patch(
            "src.api.routers.stream.is_org_member", new=AsyncMock(return_value=False)
        ),
        patch(
            "src.api.routers.stream.can_view_session", new=AsyncMock(return_value=False)
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await stream_thread(
                session_id="sess_feat_2",
                since=None,
                identity=identity,
                db=db,
            )
    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_stream_workspace_thread_non_member_no_implicit_join():
    """Workspace thread (feature_id='') non-member raises 403; add_member not called."""
    from fastapi import HTTPException
    from src.api.routers.stream import stream_thread

    ws_thread = _make_session(
        session_id="sess_ws_1",
        user_id="other_owner",
        kind="thread",
        feature_id="",
    )
    add_member_mock = AsyncMock()
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "non_member"
    identity.org_id = "org-1"

    with (
        patch(
            "src.api.routers.stream.get_session", new=AsyncMock(return_value=ws_thread)
        ),
        patch(
            "src.api.routers.stream.can_view_session", new=AsyncMock(return_value=False)
        ),
        patch("src.api.routers.stream.add_member", new=add_member_mock),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await stream_thread(
                session_id="sess_ws_1",
                since=None,
                identity=identity,
                db=db,
            )
    assert exc_info.value.status_code == 403
    add_member_mock.assert_not_awaited()


# ---------------------------------------------------------------------------
# send_message endpoint — direct-function integration tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_feature_session_org_member_gets_implicit_join():
    """Org member posting to a feature session → implicit session_members insert."""
    from src.api.routers.messages import send_message, SendMessageRequest

    feature_session = _make_session(
        session_id="sess_feat_3",
        user_id="other_owner",
        kind="thread",
        feature_id="feat-abc",
    )
    add_member_mock = AsyncMock()
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "org_member_user"
    identity.org_id = "org-1"

    request = MagicMock()

    fake_bus = MagicMock()
    fake_bus.publish = MagicMock()

    with (
        patch(
            "src.api.routers.messages.get_session",
            new=AsyncMock(return_value=feature_session),
        ),
        patch(
            "src.api.routers.messages.get_workspace_organization_id",
            new=AsyncMock(return_value="org-1"),
        ),
        patch(
            "src.api.routers.messages.is_org_member", new=AsyncMock(return_value=True)
        ),
        patch(
            "src.api.routers.messages.can_view_session",
            new=AsyncMock(return_value=True),
        ),
        patch("src.api.routers.messages.add_member", new=add_member_mock),
        patch("src.api.routers.messages.parse_mention_handles", return_value=set()),
        patch("src.api.routers.messages.resolve_mentions", return_value=[]),
        patch(
            "src.api.routers.messages.mention_candidates",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.routers.messages.append_message", new=AsyncMock(return_value=42)
        ),
        patch("src.api.routers.messages.touch_session", new=AsyncMock()),
        patch("src.api.routers.messages.set_session_title", new=AsyncMock()),
        patch("src.api.routers.messages.author_for", new=AsyncMock(return_value={})),
        patch("src.api.routers.messages.get_bus", return_value=fake_bus),
        patch("src.api.routers.messages._should_trigger_agent", return_value=False),
    ):
        body = SendMessageRequest(content="hello feature")
        resp = await send_message(
            session_id="sess_feat_3",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert resp.status_code == 202
    add_member_mock.assert_awaited_once_with(
        db, "sess_feat_3", "org_member_user", added_by="org_member_user"
    )


@pytest.mark.asyncio
async def test_send_message_workspace_thread_non_member_rejected():
    """Workspace thread (feature_id='') non-member posting → HTTPException 403."""
    from fastapi import HTTPException
    from src.api.routers.messages import send_message, SendMessageRequest

    ws_thread = _make_session(
        session_id="sess_ws_2",
        user_id="other_owner",
        kind="thread",
        feature_id="",
    )
    add_member_mock = AsyncMock()
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "non_member"
    identity.org_id = "org-1"

    request = MagicMock()

    with (
        patch(
            "src.api.routers.messages.get_session",
            new=AsyncMock(return_value=ws_thread),
        ),
        patch(
            "src.api.routers.messages.get_workspace_organization_id",
            new=AsyncMock(return_value="org-1"),
        ),
        patch(
            "src.api.routers.messages.can_view_session",
            new=AsyncMock(return_value=False),
        ),
        patch("src.api.routers.messages.add_member", new=add_member_mock),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await send_message(
                session_id="sess_ws_2",
                body=SendMessageRequest(content="try to post"),
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 403
    add_member_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_message_feature_session_non_org_member_rejected():
    """Non-org-member posting to a feature session → HTTPException 403."""
    from fastapi import HTTPException
    from src.api.routers.messages import send_message, SendMessageRequest

    feature_session = _make_session(
        session_id="sess_feat_4",
        user_id="other_owner",
        kind="thread",
        feature_id="feat-abc",
    )
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "outsider"
    identity.org_id = "org-1"

    request = MagicMock()

    with (
        patch(
            "src.api.routers.messages.get_session",
            new=AsyncMock(return_value=feature_session),
        ),
        patch(
            "src.api.routers.messages.get_workspace_organization_id",
            new=AsyncMock(return_value="org-1"),
        ),
        patch(
            "src.api.routers.messages.is_org_member", new=AsyncMock(return_value=False)
        ),
        patch(
            "src.api.routers.messages.can_view_session",
            new=AsyncMock(return_value=False),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await send_message(
                session_id="sess_feat_4",
                body=SendMessageRequest(content="should fail"),
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 403
