"""workflow_lookup_feature — read-only cross-feature lookup tool.

Exposed only when session.feature_id == '' (Channels, Team Chat threads, DMs).
Resolves a feature by ID/slug, returns title/stage/status and a short synopsis
from product-spec.md. Never exposes write paths.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "description": (
        "Look up a feature's title, lifecycle stage, status, and a short synopsis "
        "from its product spec. Accepts a feature ID or slug (e.g. 'VOY-59' or "
        "'agent-general-chat'). Scoped to the current workspace — cannot access "
        "features in other workspaces. Available only in general chat sessions "
        "(channels, threads, DMs); feature-scoped sessions already have the full "
        "feature tool set."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "feature_ref": {
                "type": "string",
                "description": (
                    "Feature ID or slug to look up (e.g. 'VOY-59' or 'agent-general-chat'). "
                    "Must contain only alphanumerics, hyphens, or underscores."
                ),
            },
        },
        "required": ["feature_ref"],
        "additionalProperties": False,
    },
}


def check_available() -> bool:
    """Return True only for non-feature sessions with workflow DB access.

    This tool is absent from feature-scoped sessions (which already have the
    richer feature tool set) and absent when the workflow DB is unreachable.
    """
    from ..db import check_workflow_available
    from ..context import get_feature_id

    return check_workflow_available() and not get_feature_id()


def _extract_synopsis(content: str) -> str:
    """Return the first non-empty non-heading paragraph from markdown content."""
    if not content:
        return ""
    paragraph_lines: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            if paragraph_lines:
                break
            continue
        if stripped.startswith("#"):
            if paragraph_lines:
                break
            continue
        paragraph_lines.append(stripped)
    synopsis = " ".join(paragraph_lines)
    return synopsis[:500] if len(synopsis) > 500 else synopsis


def handle(feature_ref: str = "", **_: Any) -> Dict[str, Any]:
    from ..context import get_workspace_id
    from ..db import (
        _validate_id,
        check_workflow_available,
        get_feature_detail,
        get_workspace_context,
    )

    if not feature_ref or not feature_ref.strip():
        return {"ok": False, "error": "feature_ref is required."}

    feature_ref = feature_ref.strip()
    try:
        _validate_id(feature_ref, "feature_ref")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    workspace_id = get_workspace_id()
    if not workspace_id:
        return {"ok": False, "error": "No workspace context for this session."}

    if not check_workflow_available():
        return {"ok": False, "error": "Workflow database is not available."}

    try:
        detail = get_feature_detail(workspace_id, feature_ref)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("workflow_lookup_feature: DB error for %r: %s", feature_ref, exc)
        return {"ok": False, "error": f"Database error: {exc}"}

    result: Dict[str, Any] = {
        "ok": True,
        "feature_ref": feature_ref,
        "title": detail.get("title", ""),
        "stage": detail.get("stage", ""),
        "status": detail.get("status", ""),
        "next_action": detail.get("next_action", ""),
        "synopsis": "",
    }

    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return result

    try:
        from ..document_repo import read_document
        from .artifacts import _resolve_management_repo

        feature_name = detail.get("feature_name") or feature_ref
        workspace_context = get_workspace_context(workspace_id)
        gh_owner, gh_repo = _resolve_management_repo(workspace_context)
        base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")
        path = f"docs/features/{feature_name}/product-spec.md"

        for branch in (
            f"feature/{feature_name}",
            f"feature/{feature_name}-init",
            base_branch,
        ):
            try:
                doc = read_document(gh_owner, gh_repo, branch, path, github_token)
                if doc.get("content"):
                    result["synopsis"] = _extract_synopsis(doc["content"])
                    break
            except Exception:
                continue
    except Exception as exc:
        logger.debug("workflow_lookup_feature: synopsis fetch failed: %s", exc)

    return result
