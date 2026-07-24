"""Resolve message author display info from user-service.

Channel/thread messages are persisted with only an ``author_id`` (X-User-Id).
For multi-user channel transcripts the FE needs a display name + avatar, so we
enrich messages here by looking up the caller's user profile / org member
directory in user-service (cached). Degrades gracefully (author name None)
when user-service is unavailable.
"""

from __future__ import annotations

import re
from typing import Any

from src.services.user_service_client import list_org_members, list_users_by_ids


def _display_name(info: dict[str, Any]) -> str | None:
    """Prefer the set display name, else the email's local part, else None."""
    name = (info.get("display_name") or "").strip()
    if name:
        return name
    email = (info.get("email") or "").strip()
    local = email.split("@")[0] if email else ""
    return local or None


def _author_obj(user_id: str, info: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user_id,
        "name": _display_name(info),
        "avatarUrl": info.get("avatar_url") or None,
        "roleLabel": info.get("role"),
    }


async def attach_authors(workspace_id: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach an ``author`` object to each user-role message (in place).

    Resolves authors by user-id (not workspace membership), so users who posted
    but aren't in the workspace member directory still get a name.
    """
    ids = [m["author_id"] for m in messages if m.get("role") == "user" and m.get("author_id")]
    if not ids:
        return messages
    users = await list_users_by_ids(ids)
    for m in messages:
        if m.get("role") == "user" and m.get("author_id"):
            m["author"] = _author_obj(m["author_id"], users.get(m["author_id"]) or {})
    return messages


async def author_for(workspace_id: str, user_id: str) -> dict[str, Any] | None:
    """Return a single author object for user_id, resolved by id."""
    if not user_id:
        return None
    users = await list_users_by_ids([user_id])
    return _author_obj(user_id, users.get(user_id) or {})


def handle_for(info: dict[str, Any]) -> str:
    """Derive a stable @mention handle from a member: email local-part, else a
    display-name slug. MUST match the FE's deriveHandle so @tokens resolve."""
    email = (info.get("email") or "").strip()
    if email:
        local = re.sub(r"[^a-z0-9._-]", "", email.split("@")[0].lower())
        if local:
            return local
    return re.sub(r"[^a-z0-9._-]+", "", (info.get("display_name") or "").lower())


async def mention_candidates(organization_id: str) -> list[dict[str, str]]:
    """Return ``[{user_id, handle}]`` for all org members, so any of them can be
    @mentioned (not just current channel members). Returns ``[]`` when the
    workspace's organization_id couldn't be resolved (e.g. workflow-backend
    unavailable)."""
    if not organization_id:
        return []
    members = await list_org_members(organization_id)
    out: list[dict[str, str]] = []
    for uid, info in members.items():
        h = handle_for(info)
        if h:
            out.append({"user_id": uid, "handle": h})
    return out
