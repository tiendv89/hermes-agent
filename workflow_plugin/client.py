"""WorkflowClient — thin HTTP wrapper around the workflow-backend API.

All methods are synchronous (requests-based). The plugin handlers call
these in tool dispatch threads so blocking I/O is acceptable.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30

_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_id(value: str, name: str) -> None:
    """Raise ValueError if value contains characters unsafe for URL path interpolation."""
    if not _ID_RE.match(value):
        raise ValueError(f"Invalid {name}: {value!r}")


class WorkflowClient:
    """HTTP client for the workflow-backend service."""

    def __init__(self, base_url: Optional[str] = None) -> None:
        self.base_url = (base_url or os.environ.get("WORKFLOW_BACKEND_URL", "")).rstrip("/")
        if not self.base_url:
            raise ValueError(
                "WORKFLOW_BACKEND_URL is not set. "
                "Set it in the environment or pass base_url explicitly."
            )

    # ------------------------------------------------------------------
    # workspace context
    # ------------------------------------------------------------------

    def get_workspace_context(self, workspace_id: str) -> Dict[str, Any]:
        """GET /api/workspaces/{workspace_id} — returns workspace metadata."""
        _validate_id(workspace_id, "workspace_id")
        url = f"{self.base_url}/api/workspaces/{workspace_id}"
        resp = requests.get(url, timeout=_DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # feature state
    # ------------------------------------------------------------------

    def get_feature_detail(self, workspace_id: str, feature_id: str) -> Dict[str, Any]:
        """GET /api/workspaces/{workspace_id}/features/{feature_id} — returns feature metadata."""
        _validate_id(workspace_id, "workspace_id")
        _validate_id(feature_id, "feature_id")
        url = f"{self.base_url}/api/workspaces/{workspace_id}/features/{feature_id}"
        resp = requests.get(url, timeout=_DEFAULT_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
