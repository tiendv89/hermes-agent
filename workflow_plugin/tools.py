"""Tool schemas and handlers for the workflow plugin.

Read tools (v1):
    workflow_get_workspace_context  — reads workspace.yaml via workflow-backend
    workflow_get_feature_state      — reads feature status + artifact Markdown

Write tools (v2, implemented in T5):
    workflow_write_product_spec
    workflow_write_technical_design
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from .client import WorkflowClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# JSON schemas
# ---------------------------------------------------------------------------

WS_CONTEXT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {
            "type": "string",
            "description": "The workspace identifier (from workspace.yaml).",
        },
    },
    "required": ["workspace_id"],
    "additionalProperties": False,
}

FEATURE_STATE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {
            "type": "string",
            "description": "The workspace identifier.",
        },
        "feature_id": {
            "type": "string",
            "description": "The feature identifier (directory name under docs/features/).",
        },
    },
    "required": ["workspace_id", "feature_id"],
    "additionalProperties": False,
}

WRITE_SPEC_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {
            "type": "string",
            "description": "The workspace identifier.",
        },
        "feature_id": {
            "type": "string",
            "description": "The feature identifier.",
        },
        "content": {
            "type": "string",
            "description": "Full Markdown content to write to product-spec.md.",
        },
        "commit_message": {
            "type": "string",
            "description": "Git commit message (optional, defaults to 'docs: update product spec').",
        },
    },
    "required": ["workspace_id", "feature_id", "content"],
    "additionalProperties": False,
}

WRITE_TD_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "workspace_id": {
            "type": "string",
            "description": "The workspace identifier.",
        },
        "feature_id": {
            "type": "string",
            "description": "The feature identifier.",
        },
        "content": {
            "type": "string",
            "description": "Full Markdown content to write to technical-design.md.",
        },
        "commit_message": {
            "type": "string",
            "description": "Git commit message (optional, defaults to 'docs: update technical design').",
        },
    },
    "required": ["workspace_id", "feature_id", "content"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Availability guard
# ---------------------------------------------------------------------------

def check_workflow_available(**_kwargs: Any) -> bool:
    """Return True only when WORKFLOW_BACKEND_URL is configured."""
    return bool(os.environ.get("WORKFLOW_BACKEND_URL", "").strip())


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def handle_get_workspace_context(workspace_id: str, **_kwargs: Any) -> Dict[str, Any]:
    """Return workspace metadata (repos, roles, model_policy) from workflow-backend."""
    try:
        client = WorkflowClient()
        data = client.get_workspace_context(workspace_id)
        return {"ok": True, "workspace": data}
    except Exception as exc:
        logger.warning("workflow_get_workspace_context failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def handle_get_feature_state(
    workspace_id: str,
    feature_id: str,
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Return feature lifecycle state plus available artifact excerpts."""
    try:
        client = WorkflowClient()
        detail = client.get_feature_detail(workspace_id, feature_id)
        return {"ok": True, "feature": detail}
    except Exception as exc:
        logger.warning("workflow_get_feature_state failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def handle_write_product_spec(
    workspace_id: str,
    feature_id: str,
    content: str,
    commit_message: str = "docs: update product spec",
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Write product-spec.md to the management repo via the GitHub Contents API.

    Implemented in T5. Placeholder so the schema is registered at startup.
    """
    return {
        "ok": False,
        "error": "workflow_write_product_spec is not yet implemented (T5).",
    }


def handle_write_technical_design(
    workspace_id: str,
    feature_id: str,
    content: str,
    commit_message: str = "docs: update technical design",
    **_kwargs: Any,
) -> Dict[str, Any]:
    """Write technical-design.md to the management repo via the GitHub Contents API.

    Implemented in T5. Placeholder so the schema is registered at startup.
    """
    return {
        "ok": False,
        "error": "workflow_write_technical_design is not yet implemented (T5).",
    }
