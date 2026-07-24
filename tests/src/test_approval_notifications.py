"""Unit tests for src/services/approval_notifications.py and the
build_approval_payload / STAGE_CATEGORY helpers in notification_client.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

# ---------------------------------------------------------------------------
# build_approval_payload
# ---------------------------------------------------------------------------


def test_build_approval_payload_product_spec():
    from src.services.notification_client import build_approval_payload

    p = build_approval_payload(
        workspace_id="ws-1",
        user_id="usr-2",
        feature_id="feat-1",
        stage="product_spec",
        actor_user_id="usr-1",
        actor_name="Duke Tran",
    )
    assert p["category"] == "spec_approved"
    assert p["source_type"] == "feature"
    assert p["source_id"] == "feat-1"
    assert p["feature_id"] == "feat-1"
    assert p["link"] == "/feature/feat-1"
    assert p["summary"] == "Duke Tran approved the product spec"
    assert p["actor_user_id"] == "usr-1"


def test_build_approval_payload_technical_design_without_actor_name():
    from src.services.notification_client import build_approval_payload

    p = build_approval_payload(
        workspace_id="ws-1",
        user_id="usr-2",
        feature_id="feat-1",
        stage="technical_design",
    )
    assert p["category"] == "design_approved"
    assert p["summary"] == "Someone approved the technical design"
    assert "actor_user_id" not in p


def test_build_approval_payload_tasks():
    from src.services.notification_client import build_approval_payload

    p = build_approval_payload(
        workspace_id="ws-1",
        user_id="usr-2",
        feature_id="feat-1",
        stage="tasks",
        actor_name="Pye Tran",
    )
    assert p["category"] == "tasks_approved"
    assert p["summary"] == "Pye Tran approved the task breakdown"


def test_stage_category_excludes_handoff():
    from src.services.notification_client import STAGE_CATEGORY

    assert "handoff" not in STAGE_CATEGORY
    assert set(STAGE_CATEGORY) == {"product_spec", "technical_design", "tasks"}


# ---------------------------------------------------------------------------
# notify_stage_approved
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_notify_stage_approved_excludes_actor_and_notifies_rest():
    from src.services.approval_notifications import notify_stage_approved

    with (
        patch(
            "src.services.approval_notifications.get_workspace_organization_id",
            AsyncMock(return_value="org-1"),
        ),
        patch(
            "src.services.approval_notifications.list_org_members",
            AsyncMock(
                return_value={"actor-1": {}, "usr-2": {}, "usr-3": {}}
            ),
        ),
        patch(
            "src.services.approval_notifications.author_for",
            AsyncMock(return_value={"name": "Duke Tran"}),
        ),
        patch("src.services.approval_notifications.schedule_notifications_bulk") as mock_bulk,
    ):
        await notify_stage_approved("ws-1", "feat-1", "product_spec", "actor-1", "org-1")

    mock_bulk.assert_called_once()
    payloads = mock_bulk.call_args[0][0]
    user_ids = {p["user_id"] for p in payloads}
    assert user_ids == {"usr-2", "usr-3"}
    assert "actor-1" not in user_ids
    for p in payloads:
        assert p["category"] == "spec_approved"
        assert p["summary"] == "Duke Tran approved the product spec"


@pytest.mark.asyncio
async def test_notify_stage_approved_noop_for_unmapped_stage():
    from src.services.approval_notifications import notify_stage_approved

    with (
        patch(
            "src.services.approval_notifications.get_workspace_organization_id"
        ) as mock_get_org,
        patch("src.services.approval_notifications.schedule_notifications_bulk") as mock_bulk,
    ):
        await notify_stage_approved("ws-1", "feat-1", "handoff", "actor-1", "org-1")

    mock_bulk.assert_not_called()
    mock_get_org.assert_not_called()


@pytest.mark.asyncio
async def test_notify_stage_approved_noop_without_actor():
    from src.services.approval_notifications import notify_stage_approved

    with (
        patch(
            "src.services.approval_notifications.get_workspace_organization_id"
        ) as mock_get_org,
        patch("src.services.approval_notifications.schedule_notifications_bulk") as mock_bulk,
    ):
        await notify_stage_approved("ws-1", "feat-1", "tasks", None, "org-1")

    mock_bulk.assert_not_called()
    mock_get_org.assert_not_called()


@pytest.mark.asyncio
async def test_notify_stage_approved_noop_when_org_unresolved():
    from src.services.approval_notifications import notify_stage_approved

    with (
        patch(
            "src.services.approval_notifications.get_workspace_organization_id",
            AsyncMock(return_value=""),
        ),
        patch("src.services.approval_notifications.schedule_notifications_bulk") as mock_bulk,
    ):
        await notify_stage_approved("ws-1", "feat-1", "tasks", "actor-1", "org-1")

    mock_bulk.assert_not_called()


@pytest.mark.asyncio
async def test_notify_stage_approved_noop_when_no_other_members():
    from src.services.approval_notifications import notify_stage_approved

    with (
        patch(
            "src.services.approval_notifications.get_workspace_organization_id",
            AsyncMock(return_value="org-1"),
        ),
        patch(
            "src.services.approval_notifications.list_org_members",
            AsyncMock(return_value={"actor-1": {}}),
        ),
        patch("src.services.approval_notifications.schedule_notifications_bulk") as mock_bulk,
    ):
        await notify_stage_approved("ws-1", "feat-1", "tasks", "actor-1", "org-1")

    mock_bulk.assert_not_called()


@pytest.mark.asyncio
async def test_notify_stage_approved_swallows_errors():
    """Must never raise — this runs fire-and-forget after an HTTP response."""
    from src.services.approval_notifications import notify_stage_approved

    with patch(
        "src.services.approval_notifications.get_workspace_organization_id",
        AsyncMock(side_effect=RuntimeError("workflow-backend unavailable")),
    ):
        # Must not raise.
        await notify_stage_approved("ws-1", "feat-1", "tasks", "actor-1", "org-1")
