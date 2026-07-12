"""workflow_init_feature tool — creates a new go-owned feature via workflow-backend.

The tool always sends owner="go"; the agent/user cannot override it.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

SCHEMA: Dict[str, Any] = {
    "description": (
        "Create a new feature in the current workspace. "
        'The feature will be go-owned (owner: "go") — this is the only supported '
        "orchestrator type for agent-initiated feature creation. "
        "On success, returns feature_id and init_pr_url so you can immediately "
        "continue the workflow (e.g. call write_product_spec against the new feature_id). "
        "Use this instead of navigating to the Board modal when working in a chat session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Feature name (required). Must be unique within the workspace.",
            },
            "description": {
                "type": "string",
                "description": "Optional feature description.",
            },
            "start_stage": {
                "type": "string",
                "description": "Optional starting lifecycle stage, if supported by the backend.",
            },
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


def handle(
    name: str = "",
    description: str = "",
    start_stage: str = "",
    **_: Any,
) -> Dict[str, Any]:
    from ..context import get_org_id, get_user_id, get_workspace_id
    from src.services.workflow_backend_client import (
        WorkflowBackendError,
        create_feature,
        run_async,
    )

    if not name or not name.strip():
        return {"ok": False, "error": "name is required."}

    workspace_id = get_workspace_id()
    if not workspace_id:
        return {"ok": False, "error": "No workspace context for this session."}

    caller_user_id = get_user_id()
    caller_org_id = get_org_id()

    try:
        data = run_async(
            create_feature(
                workspace_id,
                name.strip(),
                description=description.strip() if description else "",
                start_stage=start_stage.strip() if start_stage else None,
                user_id=caller_user_id,
                org_id=caller_org_id,
            )
        )
        return {
            "ok": True,
            "feature_id": data.get("feature_id") or data.get("id"),
            "init_pr_url": data.get("init_pr_url"),
            "owner": "go",
        }
    except WorkflowBackendError as exc:
        if exc.status and 400 <= exc.status < 500:
            return {"ok": False, "error": str(exc)}
        logger.warning("workflow_init_feature: backend error: %s", exc)
        return {"ok": False, "error": f"workflow-backend request failed: {exc}"}
    except Exception as exc:
        logger.warning("workflow_init_feature: unexpected error: %s", exc)
        return {"ok": False, "error": f"workflow-backend request failed: {exc}"}
