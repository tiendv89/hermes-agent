"""Tests for agent_dispatch.try_resolve_pending_clarify and its wiring into
the normal message-send paths (messages.py::send_message,
message_threads.py::post_thread_reply).

Bug being fixed: a plain chat reply to a `clarify` prompt used to go through
schedule_agent_turn like any other message. Since the turn is still "in
flight" (its worker thread is parked in clarify_gateway.wait_for_response),
it was silently coalesced into _pending_agent_turns instead of resolving the
clarify — the answer sat queued for up to the clarify timeout (1h) instead of
unblocking the agent. This must not regress.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


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
    feature_id="feat-abc",
):
    s = MagicMock()
    s.id = session_id
    s.workspace_id = workspace_id
    s.user_id = user_id
    s.kind = kind
    s.feature_id = feature_id
    s.archived = False
    s.title = "existing title"
    s.model = "claude-sonnet-5"
    return s


@pytest.fixture(autouse=True)
def _clean_clarify_and_active_runs():
    """clarify_gateway and agent_dispatch._active_runs are module-level
    global state — reset before/after every test so tests can't leak into
    each other."""
    from tools import clarify_gateway

    from src.api import agent_dispatch

    clarify_gateway._entries.clear()
    clarify_gateway._session_index.clear()
    agent_dispatch._active_runs.clear()
    yield
    clarify_gateway._entries.clear()
    clarify_gateway._session_index.clear()
    agent_dispatch._active_runs.clear()


# ---------------------------------------------------------------------------
# try_resolve_pending_clarify — unit tests
# ---------------------------------------------------------------------------


def test_no_active_run_returns_false():
    from src.api.agent_dispatch import try_resolve_pending_clarify

    assert try_resolve_pending_clarify("sess_1", "user_1", "some answer") is False


def test_active_run_but_different_user_returns_false():
    """Only the triggering user's plain-chat reply resolves it — a message
    from anyone else must not hijack/answer someone else's clarify prompt."""
    from tools import clarify_gateway

    from src.api import agent_dispatch
    from src.api.agent_dispatch import ActiveRun, try_resolve_pending_clarify

    agent_dispatch._active_runs["sess_1"] = ActiveRun(
        run_id="r1", task=None, triggered_by="triggering_user"
    )
    clarify_gateway.register(
        "cid-1", session_key="sess_1", question="Which repo?", choices=None
    )

    assert try_resolve_pending_clarify("sess_1", "other_user", "answer") is False
    # Untouched — a non-triggering user's message must not resolve it.
    assert clarify_gateway.has_pending("sess_1") is True


def test_active_run_matching_user_but_no_pending_clarify_returns_false():
    from src.api import agent_dispatch
    from src.api.agent_dispatch import ActiveRun, try_resolve_pending_clarify

    agent_dispatch._active_runs["sess_1"] = ActiveRun(
        run_id="r1", task=None, triggered_by="triggering_user"
    )

    assert try_resolve_pending_clarify("sess_1", "triggering_user", "answer") is False


def test_resolves_pending_clarify_for_triggering_user():
    from tools import clarify_gateway

    from src.api import agent_dispatch
    from src.api.agent_dispatch import ActiveRun, try_resolve_pending_clarify

    agent_dispatch._active_runs["sess_1"] = ActiveRun(
        run_id="r1", task=None, triggered_by="triggering_user"
    )
    clarify_gateway.register(
        "cid-1", session_key="sess_1", question="Which repo?", choices=None
    )

    assert try_resolve_pending_clarify("sess_1", "triggering_user", "the api repo") is True
    # resolve_gateway_clarify only signals the entry's Event; cleanup happens
    # in wait_for_response's own finally-block (the blocked waiter, not
    # simulated here) — so assert on the resolved state, not has_pending.
    entry = clarify_gateway._entries["cid-1"]
    assert entry.event.is_set()
    assert entry.response == "the api repo"


def test_resolves_choice_prompt_from_typed_number():
    """A multi-choice clarify (awaiting_text=False) still resolves from a
    plain typed reply — matching the gateway's own include_choice_prompts
    fallback, since a normal chat message has no notion of "button click"."""
    from tools import clarify_gateway

    from src.api import agent_dispatch
    from src.api.agent_dispatch import ActiveRun, try_resolve_pending_clarify

    agent_dispatch._active_runs["sess_1"] = ActiveRun(
        run_id="r1", task=None, triggered_by="triggering_user"
    )
    clarify_gateway.register(
        "cid-1",
        session_key="sess_1",
        question="Which one?",
        choices=["api-repo", "web-repo"],
    )

    assert try_resolve_pending_clarify("sess_1", "triggering_user", "2") is True


# ---------------------------------------------------------------------------
# send_message — router integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_message_resolves_clarify_instead_of_dispatching():
    from tools import clarify_gateway

    from src.api import agent_dispatch
    from src.api.agent_dispatch import ActiveRun
    from src.api.routers.messages import send_message, SendMessageRequest

    session = _make_session(session_id="sess_clarify_1", user_id="other_owner")
    agent_dispatch._active_runs["sess_clarify_1"] = ActiveRun(
        run_id="r1", task=None, triggered_by="answering_user"
    )
    clarify_gateway.register(
        "cid-1", session_key="sess_clarify_1", question="Which repo?", choices=None
    )

    db = _mock_db()
    identity = MagicMock()
    identity.user_id = "answering_user"
    identity.org_id = "org-1"
    request = MagicMock()

    schedule_mock = AsyncMock()

    with (
        patch(
            "src.api.routers.messages.get_session",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "src.api.routers.messages.authorize_thread_access",
            new=AsyncMock(return_value=(True, "org-1")),
        ),
        patch("src.api.routers.messages.add_member", new=AsyncMock()),
        patch("src.api.routers.messages.parse_mention_handles", return_value=set()),
        patch("src.api.routers.messages.resolve_mentions", return_value=[]),
        patch(
            "src.api.routers.messages.mention_candidates",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.routers.messages.append_message", new=AsyncMock(return_value=99)
        ),
        patch("src.api.routers.messages.touch_session", new=AsyncMock()),
        patch("src.api.routers.messages.set_session_title", new=AsyncMock()),
        patch("src.api.routers.messages.author_for", new=AsyncMock(return_value={})),
        patch("src.api.routers.messages.get_bus", return_value=MagicMock()),
        patch("src.api.routers.messages.schedule_agent_turn", new=schedule_mock),
    ):
        body = SendMessageRequest(content="the api repo")
        resp = await send_message(
            session_id="sess_clarify_1",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert resp.status_code == 202
    schedule_mock.assert_not_awaited()
    entry = clarify_gateway._entries["cid-1"]
    assert entry.event.is_set()
    assert entry.response == "the api repo"


@pytest.mark.asyncio
async def test_send_message_without_pending_clarify_dispatches_normally():
    """Control case: no pending clarify -> normal dispatch gate/coalescing
    behavior is unaffected by this change."""
    from src.api.routers.messages import send_message, SendMessageRequest

    session = _make_session(session_id="sess_no_clarify", user_id="other_owner")

    db = _mock_db()
    identity = MagicMock()
    identity.user_id = "some_user"
    identity.org_id = "org-1"
    request = MagicMock()

    with (
        patch(
            "src.api.routers.messages.get_session",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "src.api.routers.messages.authorize_thread_access",
            new=AsyncMock(return_value=(True, "org-1")),
        ),
        patch("src.api.routers.messages.add_member", new=AsyncMock()),
        patch("src.api.routers.messages.parse_mention_handles", return_value=set()),
        patch("src.api.routers.messages.resolve_mentions", return_value=[]),
        patch(
            "src.api.routers.messages.mention_candidates",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.routers.messages.append_message", new=AsyncMock(return_value=100)
        ),
        patch("src.api.routers.messages.touch_session", new=AsyncMock()),
        patch("src.api.routers.messages.set_session_title", new=AsyncMock()),
        patch("src.api.routers.messages.author_for", new=AsyncMock(return_value={})),
        patch("src.api.routers.messages.get_bus", return_value=MagicMock()),
        patch("src.api.routers.messages._should_trigger_agent", return_value=False),
    ):
        body = SendMessageRequest(content="just chatting")
        resp = await send_message(
            session_id="sess_no_clarify",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert resp.status_code == 202


# ---------------------------------------------------------------------------
# post_thread_reply — router integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_thread_reply_resolves_clarify_instead_of_dispatching():
    from tools import clarify_gateway

    from src.api import agent_dispatch
    from src.api.agent_dispatch import ActiveRun
    from src.api.routers.message_threads import post_thread_reply, PostThreadReplyRequest
    from src.db.models import Message

    session = _make_session(session_id="sess_clarify_2", user_id="other_owner")
    agent_dispatch._active_runs["sess_clarify_2"] = ActiveRun(
        run_id="r1", task=None, triggered_by="answering_user"
    )
    clarify_gateway.register(
        "cid-2", session_key="sess_clarify_2", question="Which env?", choices=None
    )

    db = _mock_db()
    root_msg = MagicMock(spec=Message)
    root_msg.id = 1
    root_msg.session_id = "sess_clarify_2"
    root_msg.thread_root_id = None
    db.get = AsyncMock(return_value=root_msg)

    identity = MagicMock()
    identity.user_id = "answering_user"
    identity.org_id = "org-1"
    request = MagicMock()

    schedule_mock = AsyncMock()

    with (
        patch(
            "src.api.routers.message_threads.get_session",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "src.api.routers.message_threads.authorize_thread_access",
            new=AsyncMock(return_value=(True, "org-1")),
        ),
        patch("src.api.routers.message_threads.add_member", new=AsyncMock()),
        patch(
            "src.api.routers.message_threads.parse_mention_handles", return_value=set()
        ),
        patch("src.api.routers.message_threads.resolve_mentions", return_value=[]),
        patch(
            "src.api.routers.message_threads.mention_candidates",
            new=AsyncMock(return_value={}),
        ),
        patch(
            "src.api.routers.message_threads.append_message",
            new=AsyncMock(return_value=101),
        ),
        patch("src.api.routers.message_threads.touch_session", new=AsyncMock()),
        patch("src.api.routers.message_threads.author_for", new=AsyncMock(return_value={})),
        patch("src.api.routers.message_threads.get_bus", return_value=MagicMock()),
        patch("src.api.routers.message_threads.schedule_agent_turn", new=schedule_mock),
    ):
        body = PostThreadReplyRequest(content="production")
        resp = await post_thread_reply(
            session_id="sess_clarify_2",
            message_id="1",
            body=body,
            request=request,
            identity=identity,
            db=db,
        )

    assert resp.status_code == 202
    schedule_mock.assert_not_awaited()
    entry = clarify_gateway._entries["cid-2"]
    assert entry.event.is_set()
    assert entry.response == "production"


# ---------------------------------------------------------------------------
# POST /threads/{session_id}/clarify — router-level: triggering-user-only
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_to_clarify_rejects_non_triggering_user():
    from fastapi import HTTPException

    from tools import clarify_gateway

    from src.api import agent_dispatch
    from src.api.agent_dispatch import ActiveRun
    from src.api.routers.threads import reply_to_clarify, ClarifyReplyRequest

    agent_dispatch._active_runs["sess_clarify_3"] = ActiveRun(
        run_id="r1", task=MagicMock(), triggered_by="triggering_user"
    )
    clarify_gateway.register(
        "cid-3", session_key="sess_clarify_3", question="Which env?", choices=None
    )

    identity = MagicMock()
    identity.user_id = "someone_else"
    identity.org_id = "org-1"

    with pytest.raises(HTTPException) as exc_info:
        await reply_to_clarify(
            session_id="sess_clarify_3",
            body=ClarifyReplyRequest(clarify_id="cid-3", response="prod"),
            identity=identity,
            db=_mock_db(),
        )
    assert exc_info.value.status_code == 403
    # Untouched.
    entry = clarify_gateway._entries["cid-3"]
    assert not entry.event.is_set()


@pytest.mark.asyncio
async def test_reply_to_clarify_accepts_triggering_user():
    from tools import clarify_gateway

    from src.api import agent_dispatch
    from src.api.agent_dispatch import ActiveRun
    from src.api.routers.threads import reply_to_clarify, ClarifyReplyRequest

    agent_dispatch._active_runs["sess_clarify_4"] = ActiveRun(
        run_id="r1", task=MagicMock(), triggered_by="triggering_user"
    )
    clarify_gateway.register(
        "cid-4", session_key="sess_clarify_4", question="Which env?", choices=None
    )

    identity = MagicMock()
    identity.user_id = "triggering_user"
    identity.org_id = "org-1"

    resp = await reply_to_clarify(
        session_id="sess_clarify_4",
        body=ClarifyReplyRequest(clarify_id="cid-4", response="prod"),
        identity=identity,
        db=_mock_db(),
    )
    assert resp.status_code == 202
    entry = clarify_gateway._entries["cid-4"]
    assert entry.event.is_set()
    assert entry.response == "prod"


# ---------------------------------------------------------------------------
# GET /threads/{session_id}/clarify — peek at the pending prompt
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_pending_clarify_returns_the_entry_when_authorized():
    from tools import clarify_gateway

    from src.api.routers.threads import get_pending_clarify

    session = _make_session(session_id="sess_clarify_5")
    clarify_gateway.register(
        "cid-5",
        session_key="sess_clarify_5",
        question="Which repo?",
        choices=["api-repo", "web-repo"],
    )

    identity = MagicMock()
    identity.user_id = "any_member"
    identity.org_id = "org-1"

    with (
        patch(
            "src.api.routers.threads.get_session",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "src.api.routers.threads.authorize_thread_access",
            new=AsyncMock(return_value=(True, "org-1")),
        ),
    ):
        resp = await get_pending_clarify(
            session_id="sess_clarify_5", identity=identity, db=_mock_db()
        )

    assert resp.status_code == 200
    import json

    payload = json.loads(resp.body)
    assert payload["clarify_id"] == "cid-5"
    assert payload["question"] == "Which repo?"
    assert payload["choices"] == ["api-repo", "web-repo"]


@pytest.mark.asyncio
async def test_get_pending_clarify_404_when_nothing_pending():
    from fastapi import HTTPException

    from src.api.routers.threads import get_pending_clarify

    session = _make_session(session_id="sess_clarify_6")

    identity = MagicMock()
    identity.user_id = "any_member"
    identity.org_id = "org-1"

    with (
        patch(
            "src.api.routers.threads.get_session",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "src.api.routers.threads.authorize_thread_access",
            new=AsyncMock(return_value=(True, "org-1")),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_pending_clarify(
                session_id="sess_clarify_6", identity=identity, db=_mock_db()
            )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "no_pending_clarify"


@pytest.mark.asyncio
async def test_get_pending_clarify_requires_thread_access():
    """A caller with no access to the thread must not be able to peek at its
    pending clarify — authorize_thread_access's own exception propagates."""
    from fastapi import HTTPException

    from src.api.routers.threads import get_pending_clarify

    session = _make_session(session_id="sess_clarify_7")

    identity = MagicMock()
    identity.user_id = "outsider"
    identity.org_id = "org-1"

    with (
        patch(
            "src.api.routers.threads.get_session",
            new=AsyncMock(return_value=session),
        ),
        patch(
            "src.api.routers.threads.authorize_thread_access",
            new=AsyncMock(side_effect=HTTPException(status_code=403, detail="Not a member of this thread.")),
        ),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await get_pending_clarify(
                session_id="sess_clarify_7", identity=identity, db=_mock_db()
            )

    assert exc_info.value.status_code == 403
