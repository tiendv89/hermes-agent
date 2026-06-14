"""Mention parsing and resolution for @-tokens in chat messages (v4 team-chat)."""

from __future__ import annotations

import re
from typing import Any, Dict, List

AGENT_HANDLE = "agent"
_MENTION_RE = re.compile(r"@(\w+(?:[._-]\w+)*)")


def parse_mention_handles(content: str) -> List[str]:
    """Extract @handle tokens from content (lowercased, order-preserving, deduped)."""
    seen: Dict[str, None] = {}
    for m in _MENTION_RE.findall(content):
        seen.setdefault(m.lower(), None)
    return list(seen)


def mentions_agent(content: str) -> bool:
    """True if content contains an explicit @agent mention."""
    return AGENT_HANDLE in parse_mention_handles(content)


def resolve_mentions(
    handles: List[str],
    members: List[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """Resolve @handle tokens to mention dicts.

    members: list of dicts with 'user_id' and optionally 'handle' / 'username'.
    @agent is always resolved as the agent sentinel regardless of membership.
    Unknown handles are silently skipped (per test plan).

    Returns list of {'mentioned_id': ..., 'mentioned_kind': 'user'|'agent'}.
    """
    handle_map: Dict[str, str] = {}
    for m in members:
        h = (m.get("handle") or m.get("username") or "").strip().lower()
        if h:
            handle_map[h] = m["user_id"]

    resolved: List[Dict[str, str]] = []
    seen_ids: Dict[str, None] = {}

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
