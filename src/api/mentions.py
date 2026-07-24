"""Mention parsing and resolution for @-tokens in chat messages (v4 team-chat)."""

from __future__ import annotations

import re
from typing import Any

AGENT_HANDLE = "agent"

# Matches EITHER the historical bare "@handle" form OR the canonical
# "<p:handle>" tag form (see workflow-extension/digital-factory-ui's shared
# mention-tag format — mirrored here, not code-shared, since this is a
# different language/repo). Both forms remain live on the wire: <p:handle>
# is what the composer's people-mention picker inserts now, but bare
# "@handle" text is still produced by digital-factory-ui's own "auto-tag
# @agent for a channel message" convenience logic (agent-chat-panel.tsx's
# handleCtaAction / slash-command dispatch, which prepends a literal
# "@agent " string, not a tag) and can still be typed by hand without ever
# triggering the picker — so this must keep recognizing both, not just the
# newer tag form.
_MENTION_RE = re.compile(r"@(\w+(?:[._-]\w+)*)|<p:(\w+(?:[._-]\w+)*)>")


def parse_mention_handles(content: str) -> list[str]:
    """Extract @handle / <p:handle> tokens from content (lowercased, order-preserving, deduped)."""
    seen: dict[str, None] = {}
    for bare, tagged in _MENTION_RE.findall(content):
        handle = bare or tagged
        seen.setdefault(handle.lower(), None)
    return list(seen)


def mentions_agent(content: str) -> bool:
    """True if content contains an explicit @agent mention."""
    return AGENT_HANDLE in parse_mention_handles(content)


def resolve_mentions(
    handles: list[str],
    members: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Resolve @handle tokens to mention dicts.

    members: list of dicts with 'user_id' and optionally 'handle' / 'username'.
    @agent is always resolved as the agent sentinel regardless of membership.
    Unknown handles are silently skipped (per test plan).

    Returns list of {'mentioned_id': ..., 'mentioned_kind': 'user'|'agent'}.
    """
    handle_map: dict[str, str] = {}
    for m in members:
        h = (m.get("handle") or m.get("username") or "").strip().lower()
        if h:
            handle_map[h] = m["user_id"]

    resolved: list[dict[str, str]] = []
    seen_ids: dict[str, None] = {}

    for handle in handles:
        if handle == AGENT_HANDLE:
            if "agent" not in seen_ids:
                resolved.append({"mentioned_id": "agent", "mentioned_kind": "agent"})
                seen_ids["agent"] = None
        elif handle in handle_map:
            uid = handle_map[handle]
            if uid not in seen_ids:
                resolved.append({"mentioned_id": uid, "mentioned_kind": "user"})
                seen_ids[uid] = None
        # Unknown handle: silently skip

    return resolved
