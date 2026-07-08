"""HTTP client for storage-service document read/write (service-to-service).

hermes-agent calls storage-service directly (no BFF) to read/write documents
for go-owned features. ts-owned features continue to use document_repo (git).

Configuration (env vars):
  STORAGE_SERVICE_URL    Base URL of storage-service, e.g. http://storage-service:8090.
                         If unset, raises StorageServiceError(reason_code="missing_config").
  STORAGE_SERVICE_TOKEN  Bearer token accepted by storage-service's RequireBFFIdentity.
                         If unset, same error.

Endpoint contract (storage-service, T6):
  GET  {STORAGE_SERVICE_URL}/api/workspaces/{wid}/features/{fid}/documents/{kind}/content
  PUT  {STORAGE_SERVICE_URL}/api/workspaces/{wid}/features/{fid}/documents/{kind}/content
  Headers:
    Authorization: Bearer <STORAGE_SERVICE_TOKEN>
    X-User-Id: <caller user_id>
    X-Org-Id: <caller org_id>
    X-Accessible-Org-Ids: <org_id>
  GET response: {"content": "...", "version_id": "..."}
  PUT body:     {"content": "..."}
  PUT response: {"ok": true, "version_id": "..."}
  → 4xx         {"error": "<reason_code>", "message": "..."}
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

import requests

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30

# Valid document kinds for storage-service documents.
DOCUMENT_KINDS = frozenset({"product_spec", "technical_design", "tasks", "handoff"})


class StorageServiceError(Exception):
    """Raised when storage-service returns a non-2xx response or is misconfigured.

    Attributes:
        reason_code: Machine-readable code (e.g. ``missing_config``,
            ``document_not_found``) or empty string.
        status: HTTP status code, 0 when the error is local.
    """

    def __init__(self, message: str, *, reason_code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status


def _resolve_config() -> tuple[str, str]:
    """Return (base_url, token) or raise StorageServiceError(missing_config)."""
    url = os.environ.get("STORAGE_SERVICE_URL", "").strip()
    token = os.environ.get("STORAGE_SERVICE_TOKEN", "").strip()
    if not url or not token:
        raise StorageServiceError(
            "STORAGE_SERVICE_URL and STORAGE_SERVICE_TOKEN must both be set to proxy "
            "document reads/writes for go-owned features.",
            reason_code="missing_config",
        )
    return url.rstrip("/"), token


def _build_headers(token: str, user_id: str, org_id: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-User-Id": user_id or "",
        "X-Org-Id": org_id or "",
        "X-Accessible-Org-Ids": org_id or "",
    }


def _content_url(base_url: str, workspace_id: str, feature_id: str, kind: str) -> str:
    return f"{base_url}/api/workspaces/{workspace_id}/features/{feature_id}/documents/{kind}/content"


def read_document_content(
    workspace_id: str,
    feature_id: str,
    kind: str,
    *,
    user_id: str = "",
    org_id: str = "",
) -> Dict[str, Any]:
    """Read a document's content from storage-service.

    Returns a dict with keys:
      - ``content`` (str): document markdown content, or ``""`` when not found
      - ``version_id`` (str | None): current version id

    Raises StorageServiceError on config errors or non-2xx/404 responses.
    """
    base_url, token = _resolve_config()
    url = _content_url(base_url, workspace_id, feature_id, kind)
    headers = _build_headers(token, user_id, org_id)
    try:
        resp = requests.get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise StorageServiceError(f"storage-service request failed: {exc}", reason_code="request_error") from exc

    if resp.status_code == 404:
        return {"content": "", "version_id": None}

    if not resp.ok:
        body: Any = {}
        try:
            body = resp.json()
        except Exception:
            pass
        reason = body.get("error") or body.get("reason_code") or ""
        raise StorageServiceError(
            f"storage-service GET {url} returned {resp.status_code}: {body}",
            reason_code=reason,
            status=resp.status_code,
        )

    data = resp.json()
    return {
        "content": data.get("content", ""),
        "version_id": data.get("version_id"),
    }


def write_document_content(
    workspace_id: str,
    feature_id: str,
    kind: str,
    content: str,
    *,
    user_id: str = "",
    org_id: str = "",
) -> Dict[str, Any]:
    """Write a document's content to storage-service.

    Returns a dict with keys:
      - ``ok`` (bool): True on success
      - ``version_id`` (str | None): new version id

    Raises StorageServiceError on config errors or non-2xx responses.
    """
    base_url, token = _resolve_config()
    url = _content_url(base_url, workspace_id, feature_id, kind)
    headers = _build_headers(token, user_id, org_id)
    payload = {"content": content}
    try:
        resp = requests.put(url, headers=headers, json=payload, timeout=_DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise StorageServiceError(f"storage-service request failed: {exc}", reason_code="request_error") from exc

    if not resp.ok:
        body: Any = {}
        try:
            body = resp.json()
        except Exception:
            pass
        reason = body.get("error") or body.get("reason_code") or ""
        raise StorageServiceError(
            f"storage-service PUT {url} returned {resp.status_code}: {body}",
            reason_code=reason,
            status=resp.status_code,
        )

    data = resp.json()
    return {
        "ok": True,
        "version_id": data.get("version_id"),
    }
