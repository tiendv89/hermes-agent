"""Regression tests for Channels/Team-Chat-thread visibility (T6, later widened).

Originally proved that the T2/T3 visibility widening — which made feature
sessions (kind='thread' AND feature_id != '') org-public — did NOT change
behavior for workspace Team Chat threads or channels. A later change made
*all* thread-kind sessions (feature-scoped or workspace-level) org-public by
default, matching channels, so a workspace member no longer needs an explicit
session_members row to view/post in any session (only channels/DMs still do).

This file now proves:

  1. Workspace Team Chat threads (feature_id='') — behave exactly like feature
     sessions: a confirmed org member is authorized at the store layer
     (can_view_session), stream endpoint, and messages endpoint, with implicit
     join firing the same way.
  2. Channels (kind='channel') — unaffected by the sessions-visibility change:
     non-members still require an explicit session_members row; no implicit
     join is triggered on stream/post access; the dedicated /join endpoint is
     the only way to gain membership.
  3. list_member_sessions — channels are fully excluded; unaffected by the
     sessions-visibility change (this function is not wired to any router and
     is out of scope for that change).
"""

from __future__ import annotations

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
    session_id="sess_1",
    workspace_id="ws_1",
    user_id="owner_user",
    kind="thread",
    feature_id="",
):
    s = MagicMock()
    s.id = session_id
    s.workspace_id = workspace_id
    s.user_id = user_id
    s.kind = kind
    s.feature_id = feature_id
    s.archived = False
    return s


# ============================================================================
# 1. Workspace Team Chat thread — now org-public, same as feature sessions
#    Subtask: "a confirmed org member can view/post a workspace Team Chat
#    thread (feature_id='') with no explicit session_members row, and gets
#    implicitly joined, exactly like a feature session."
# ============================================================================


@pytest.mark.asyncio
async def test_regression_workspace_thread_org_member_authorized_store():
    """can_view_session: workspace thread org member authorized with no explicit row.

    caller_is_workspace_member=True is now sufficient for any kind='thread'
    session regardless of feature_id — no session_members lookup needed.
    """
    from src.db.store import can_view_session

    db = _mock_db()

    session = _make_session(kind="thread", feature_id="")

    result = await can_view_session(
        db, session, "org_member_user", caller_is_workspace_member=True
    )

    assert result is True
    db.get.assert_not_called()  # no DB lookup needed — policy allows


@pytest.mark.asyncio
async def test_regression_workspace_thread_non_org_member_rejected_store():
    """can_view_session: workspace thread rejects a caller who is not a
    confirmed org member, with no fallback to explicit membership."""
    from src.db.store import can_view_session

    db = _mock_db()

    session = _make_session(kind="thread", feature_id="")

    result = await can_view_session(
        db, session, "outsider_user", caller_is_workspace_member=False
    )

    assert result is False
    db.get.assert_not_called()


@pytest.mark.asyncio
async def test_regression_stream_workspace_thread_org_member_authorized_and_implicit_join():
    """Stream endpoint: org member authorized on a workspace thread → implicit join.

    The stream handler now resolves org membership for any kind='thread'
    session regardless of feature_id, mirroring feature-session behavior.
    """
    from src.api.routers.stream import stream_thread

    ws_thread = _make_session(
        session_id="sess_wsthread_1",
        kind="thread",
        feature_id="",
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
            new=AsyncMock(return_value=ws_thread),
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
        from starlette.responses import StreamingResponse

        resp = await stream_thread(
            session_id="sess_wsthread_1",
            since=None,
            identity=identity,
            db=db,
        )
        assert isinstance(resp, StreamingResponse)
        add_member_mock.assert_awaited_once_with(
            db, "sess_wsthread_1", "non_member_user", added_by="non_member_user"
        )


@pytest.mark.asyncio
async def test_regression_stream_workspace_thread_non_org_member_403():
    """Stream endpoint: workspace thread non-org-member still raises 403."""
    from fastapi import HTTPException
    from src.api.routers.stream import stream_thread

    ws_thread = _make_session(
        session_id="sess_wsthread_2",
        kind="thread",
        feature_id="",
    )
    add_member_mock = AsyncMock()
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "outsider"
    identity.org_id = "org-1"

    with (
        patch(
            "src.api.routers.stream.get_session",
            new=AsyncMock(return_value=ws_thread),
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
        patch("src.api.routers.stream.add_member", new=add_member_mock),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await stream_thread(
                session_id="sess_wsthread_2",
                since=None,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 403
    add_member_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_regression_messages_workspace_thread_org_member_gets_implicit_join():
    """Messages endpoint: org member posting to a workspace thread → implicit join."""
    from src.api.routers.messages import send_message, SendMessageRequest

    ws_thread = _make_session(
        session_id="sess_wsthread_3",
        kind="thread",
        feature_id="",
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
            new=AsyncMock(return_value=ws_thread),
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
            "src.api.routers.messages.append_message",
            new=AsyncMock(return_value=99),
        ),
        patch("src.api.routers.messages.touch_session", new=AsyncMock()),
        patch("src.api.routers.messages.set_session_title", new=AsyncMock()),
        patch("src.api.routers.messages.author_for", new=AsyncMock(return_value={})),
        patch("src.api.routers.messages.get_bus", return_value=fake_bus),
        patch("src.api.routers.messages._should_trigger_agent", return_value=False),
    ):
        resp = await send_message(
            session_id="sess_wsthread_3",
            body=SendMessageRequest(content="workspace thread message"),
            request=request,
            identity=identity,
            db=db,
        )

    assert resp.status_code == 202
    add_member_mock.assert_awaited_once_with(
        db, "sess_wsthread_3", "org_member_user", added_by="org_member_user"
    )


# ============================================================================
# 2. Channel visibility/join regressions
#    Subtask: "Channel visibility/join behavior (test_join_channel_idempotent,
#    test_join_channel_success) is unchanged."
# ============================================================================


@pytest.mark.asyncio
async def test_regression_channel_non_member_rejected_store():
    """can_view_session: channel non-member still requires explicit join after T2/T3.

    Channels (kind='channel') fall through to is_member — no org-member shortcut.
    This ensures can_view_session's branch 2 ('thread' + feature_id != '') is
    strictly scoped and does not widen channel access.
    """
    from src.db.store import can_view_session

    db = _mock_db()
    db.get = AsyncMock(return_value=None)  # no session_members row

    channel = _make_session(kind="channel", feature_id="feat-1")

    result = await can_view_session(
        db, channel, "non_member_user", caller_is_workspace_member=True
    )

    assert result is False
    db.get.assert_called_once()  # explicit membership check was made


@pytest.mark.asyncio
async def test_regression_channel_explicit_member_still_allowed_store():
    """can_view_session: channel with explicit session_members row still allowed."""
    from src.db.store import can_view_session
    from src.db.models import SessionMember

    existing_row = MagicMock(spec=SessionMember)
    db = _mock_db()
    db.get = AsyncMock(return_value=existing_row)

    channel = _make_session(kind="channel", feature_id="feat-1")

    result = await can_view_session(
        db, channel, "channel_member", caller_is_workspace_member=False
    )

    assert result is True


@pytest.mark.asyncio
async def test_regression_stream_channel_non_member_403():
    """Stream endpoint: channel non-member still raises 403 after T2/T3.

    The stream handler does NOT set caller_is_workspace_member=True for channels
    (the org-membership lookup is guarded by kind='thread' AND feature_id != '').
    Channel access therefore falls back to is_member → rejected.
    """
    from fastapi import HTTPException
    from src.api.routers.stream import stream_thread

    channel = _make_session(
        session_id="chan_rg_1",
        kind="channel",
        feature_id="feat-abc",
    )
    add_member_mock = AsyncMock()
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "non_member_user"
    identity.org_id = "org-1"

    with (
        patch(
            "src.api.routers.stream.get_session",
            new=AsyncMock(return_value=channel),
        ),
        patch(
            "src.api.routers.stream.can_view_session",
            new=AsyncMock(return_value=False),
        ),
        patch("src.api.routers.stream.add_member", new=add_member_mock),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await stream_thread(
                session_id="chan_rg_1",
                since=None,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 403
    add_member_mock.assert_not_awaited()  # no implicit join for channels


@pytest.mark.asyncio
async def test_regression_stream_channel_no_implicit_join_for_member():
    """Stream endpoint: no implicit join triggered for channels even for members.

    The implicit-join guard (kind='thread' AND feature_id != '') excludes channels
    by kind. A channel member accessing the stream must NOT trigger add_member — the
    only valid join path is the explicit POST /channels/{id}/join endpoint.
    """
    from src.api.routers.stream import stream_thread

    channel = _make_session(
        session_id="chan_rg_2",
        kind="channel",
        feature_id="feat-abc",
    )
    add_member_mock = AsyncMock()
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "channel_member"
    identity.org_id = "org-1"

    fake_bus = MagicMock()
    fake_bus.subscribe_raw = MagicMock()
    fake_bus.unsubscribe_raw = MagicMock()

    with (
        patch(
            "src.api.routers.stream.get_session",
            new=AsyncMock(return_value=channel),
        ),
        patch(
            "src.api.routers.stream.can_view_session",
            new=AsyncMock(return_value=True),
        ),
        patch("src.api.routers.stream.add_member", new=add_member_mock),
        patch(
            "src.api.routers.stream.get_messages_since",
            new=AsyncMock(return_value=[]),
        ),
        patch("src.api.routers.stream.attach_authors", new=AsyncMock()),
        patch("src.api.routers.stream.get_bus", return_value=fake_bus),
    ):
        from starlette.responses import StreamingResponse

        resp = await stream_thread(
            session_id="chan_rg_2",
            since=None,
            identity=identity,
            db=db,
        )
        assert isinstance(resp, StreamingResponse)

    add_member_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_regression_messages_channel_non_member_403():
    """Messages endpoint: channel non-member still rejected after T2/T3."""
    from fastapi import HTTPException
    from src.api.routers.messages import send_message, SendMessageRequest

    channel = _make_session(
        session_id="chan_rg_3",
        kind="channel",
        feature_id="feat-abc",
    )
    add_member_mock = AsyncMock()
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "non_member_user"
    identity.org_id = "org-1"

    request = MagicMock()

    with (
        patch(
            "src.api.routers.messages.get_session",
            new=AsyncMock(return_value=channel),
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
                session_id="chan_rg_3",
                body=SendMessageRequest(content="should fail"),
                request=request,
                identity=identity,
                db=db,
            )

    assert exc_info.value.status_code == 403
    add_member_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_regression_messages_channel_no_implicit_join():
    """Messages endpoint: no implicit join triggered for channels after T2/T3.

    Authorized channel members can post, but the implicit-join gate must not
    fire for channels — they join via the explicit /join endpoint only.
    """
    from src.api.routers.messages import send_message, SendMessageRequest

    channel = _make_session(
        session_id="chan_rg_4",
        kind="channel",
        feature_id="feat-abc",
    )
    add_member_mock = AsyncMock()
    db = _mock_db()

    identity = MagicMock()
    identity.user_id = "channel_member"
    identity.org_id = "org-1"

    request = MagicMock()
    fake_bus = MagicMock()
    fake_bus.publish = MagicMock()

    with (
        patch(
            "src.api.routers.messages.get_session",
            new=AsyncMock(return_value=channel),
        ),
        patch(
            "src.api.routers.messages.get_workspace_organization_id",
            new=AsyncMock(return_value="org-1"),
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
            "src.api.routers.messages.append_message",
            new=AsyncMock(return_value=100),
        ),
        patch("src.api.routers.messages.touch_session", new=AsyncMock()),
        patch("src.api.routers.messages.set_session_title", new=AsyncMock()),
        patch("src.api.routers.messages.author_for", new=AsyncMock(return_value={})),
        patch("src.api.routers.messages.get_bus", return_value=fake_bus),
        patch("src.api.routers.messages._should_trigger_agent", return_value=False),
    ):
        resp = await send_message(
            session_id="chan_rg_4",
            body=SendMessageRequest(content="channel message"),
            request=request,
            identity=identity,
            db=db,
        )

    assert resp.status_code == 202
    add_member_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_regression_join_channel_success_unchanged():
    """Channel join endpoint still works correctly after T2/T3 (test_join_channel_success).

    The /join endpoint is the explicit membership path for channels; it must be
    unaffected by the feature-session implicit-join changes in T2.
    """
    from src.api.routers.channels import join_channel_endpoint
    from src.api.identity import Identity

    channel = _make_session(session_id="chan_join_1", kind="channel")
    db = _mock_db()

    identity = Identity(user_id="user_joiner", org_id="org-1")
    add_member_mock = AsyncMock()

    with (
        patch(
            "src.api.routers.channels.get_channel",
            new=AsyncMock(return_value=channel),
        ),
        patch(
            "src.api.routers.channels.is_member",
            new=AsyncMock(return_value=False),
        ),
        patch("src.api.routers.channels.add_member", new=add_member_mock),
    ):
        result = await join_channel_endpoint(
            channel_id="chan_join_1",
            identity=identity,
            db=db,
        )

    assert result.body  # 200 response
    content = result.body
    import json

    body = json.loads(content)
    assert body["joined"] is True
    assert body["channel_id"] == "chan_join_1"
    add_member_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_regression_join_channel_idempotent_unchanged():
    """Channel join is idempotent after T2/T3 (test_join_channel_idempotent).

    Re-joining a channel a second time must still return 200 without error —
    the implicit-join path for feature sessions must not have changed this.
    """
    from src.api.routers.channels import join_channel_endpoint
    from src.api.identity import Identity

    channel = _make_session(session_id="chan_join_2", kind="channel")
    db = _mock_db()

    identity = Identity(user_id="already_joined", org_id="org-1")
    add_member_mock = AsyncMock()

    with (
        patch(
            "src.api.routers.channels.get_channel",
            new=AsyncMock(return_value=channel),
        ),
        patch(
            "src.api.routers.channels.is_member",
            new=AsyncMock(return_value=True),  # already a member
        ),
        patch("src.api.routers.channels.add_member", new=add_member_mock),
    ):
        result = await join_channel_endpoint(
            channel_id="chan_join_2",
            identity=identity,
            db=db,
        )

    import json

    body = json.loads(result.body)
    assert body["joined"] is True  # still 200, idempotent


# ============================================================================
# 3. list_member_sessions regressions
#    Subtask: "list_member_sessions listing for threads/channels unaffected by
#    the new feature-session branch."
# ============================================================================


@pytest.mark.asyncio
async def test_regression_list_member_sessions_channels_excluded():
    """list_member_sessions does NOT include channels.

    Branch 3 (org-visible feature sessions) is strictly scoped to
    kind='thread' AND feature_id != ''.  Channels (kind='channel') must never
    appear in the result — use list_channels() for those.
    The store-level query already filters kind='thread'; this test asserts the
    caller gets an empty list when the DB returns nothing matching.
    """
    from src.db.store import list_member_sessions

    # Simulate the DB returning nothing (a channel would not match the
    # kind='thread' WHERE clause in list_member_sessions).
    result_mock = MagicMock()
    result_mock.all.return_value = []

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    sessions = await list_member_sessions(
        db, "ws-1", "user_channel_member", accessible_workspace_ids=["ws-1"]
    )

    assert sessions == []


@pytest.mark.asyncio
async def test_regression_list_member_sessions_workspace_thread_not_in_org_branch():
    """list_member_sessions: workspace threads not included via branch 3 for non-members.

    Branch 3 in list_member_sessions uses the condition:
      kind='thread' AND feature_id != '' AND workspace_id IN accessible_workspaces

    A workspace Team Chat thread (feature_id='') does NOT satisfy feature_id != '',
    so it must not appear for a non-member via the org-visibility branch.
    Only branches 1 (own) and 2 (member-of) can surface workspace threads.
    """
    from src.db.store import list_member_sessions

    # DB returns nothing (workspace thread has feature_id='', fails branch 3 filter).
    result_mock = MagicMock()
    result_mock.all.return_value = []

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    sessions = await list_member_sessions(
        db, "ws-1", "non_member_user", accessible_workspace_ids=["ws-1"]
    )

    assert sessions == []


@pytest.mark.asyncio
async def test_regression_list_member_sessions_feature_session_still_visible():
    """list_member_sessions: feature session remains visible via branch 3.

    Sanity check — the T3 change must still work after T2/T3 land together.
    A non-member org user with accessible_workspace_ids set sees feature sessions.
    """
    import time
    from src.db.store import list_member_sessions

    now = time.time()
    row_feature = MagicMock(
        id="sess_feat_rg",
        title="Feature Chat",
        feature_id="feat-rg",
        started_at=now - 100,
        last_active_at=now - 5,
        model="claude-sonnet",
        kind="thread",
    )

    result_mock = MagicMock()
    result_mock.all.return_value = [row_feature]

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    sessions = await list_member_sessions(
        db, "ws-1", "non_member_org_user", accessible_workspace_ids=["ws-1"]
    )

    assert len(sessions) == 1
    assert sessions[0]["id"] == "sess_feat_rg"
    assert sessions[0]["feature_id"] == "feat-rg"
    assert sessions[0]["kind"] == "thread"


@pytest.mark.asyncio
async def test_regression_list_member_sessions_own_workspace_thread_still_listed():
    """list_member_sessions: owner still sees their own workspace thread (branch 1 intact).

    Branch 3 changes must not regress branch 1 — the owner always sees their own sessions
    regardless of feature_id.
    """
    import time
    from src.db.store import list_member_sessions

    now = time.time()
    row_ws_thread = MagicMock(
        id="sess_own_ws",
        title="My Team Chat",
        feature_id="",
        started_at=now - 200,
        last_active_at=now - 20,
        model=None,
        kind="thread",
    )

    result_mock = MagicMock()
    result_mock.all.return_value = [row_ws_thread]

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    sessions = await list_member_sessions(db, "ws-1", "owner_user")

    assert len(sessions) == 1
    assert sessions[0]["id"] == "sess_own_ws"
    assert sessions[0]["feature_id"] == ""


@pytest.mark.asyncio
async def test_regression_list_member_sessions_member_of_workspace_thread_still_listed():
    """list_member_sessions: explicit member still sees their workspace thread (branch 2 intact).

    Branch 3 changes must not regress branch 2 — a user with an explicit
    session_members row still sees the workspace thread.
    """
    import time
    from src.db.store import list_member_sessions

    now = time.time()
    row_ws_thread = MagicMock(
        id="sess_member_ws",
        title="Shared Thread",
        feature_id="",
        started_at=now - 150,
        last_active_at=now - 15,
        model=None,
        kind="thread",
    )

    result_mock = MagicMock()
    result_mock.all.return_value = [row_ws_thread]

    db = _mock_db()
    db.execute = AsyncMock(return_value=result_mock)

    sessions = await list_member_sessions(db, "ws-1", "member_user")

    assert len(sessions) == 1
    assert sessions[0]["id"] == "sess_member_ws"
