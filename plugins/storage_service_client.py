"""HTTP client for storage-service document/image read (service-to-service).

hermes-agent calls storage-service directly (no BFF) to read/write documents
for go-owned features, and to download chat-attached images. ts-owned
features continue to use document_repo (git).

Configuration (env vars):
  STORAGE_SERVICE_URL    Base URL of storage-service, e.g. http://storage-service:8090.
                         If unset, raises StorageServiceError(reason_code="missing_config").
  STORAGE_SERVICE_TOKEN  Bearer token accepted by storage-service's RequireBFFIdentity.
                         If unset, same error.

Endpoint contract (storage-service):
  GET  {STORAGE_SERVICE_URL}/api/workspaces/{wid}/features/{fid}/documents/content?path={path}
  PUT  {STORAGE_SERVICE_URL}/api/workspaces/{wid}/features/{fid}/documents/content?path={path}
  path is the document's relative filename within the feature folder (e.g.
  "product_spec.md", "tech_design.md", "tasks.md").

  GET  {STORAGE_SERVICE_URL}/api/workspaces/{wid}/documents/content?path={path}
  PUT  {STORAGE_SERVICE_URL}/api/workspaces/{wid}/documents/content?path={path}
  Same handler, no {fid} segment — for a document with no owning feature (a
  workspace-root file, e.g. one uploaded outside any feature's folder in the
  Files browser). Pass feature_id="" to read_document_content/
  write_document_content to hit this variant; path is then the document's
  location relative to the workspace root instead of a feature folder.

  The content PUT is edit-only: it 404s ("document not found") for any path
  that doesn't already have a document row — only the three canonical docs
  are pre-created (at feature-creation time, by workflow-backend). There is
  no create-on-missing/upsert behavior in the PUT itself.
  write_document_content handles this transparently: on a 404 it calls
  POST {STORAGE_SERVICE_URL}/api/documents ({"workspace_id", "feature_id",
  "path"}, feature_id="" for workspace-root) to create the (empty) row, then
  retries the PUT once.

  Headers:
    Authorization: Bearer <STORAGE_SERVICE_TOKEN>
    X-User-Id: <caller user_id>
    X-Org-Id: <caller org_id>
    X-Accessible-Org-Ids: <org_id>
  GET response: {"content": "...", "version_id": "..."}
  PUT body:     {"content": "..."}
  PUT response: {"ok": true, "version_id": "..."}
  → 4xx         {"error": "<reason_code>", "message": "..."}

  GET  {STORAGE_SERVICE_URL}/api/workspaces/{wid}/images/{image_id}
  Same headers as above (no Content-Type). Returns the raw image bytes with
  its real Content-Type — see download_image. Deliberately NOT handed to
  vision_analyze_tool as a URL: storage-service is internal-only, so the
  tool's SSRF guard (tools/url_safety.py) would reject fetching it directly.
  Callers should download the bytes here and pass a local file path instead
  (see agent_dispatch.py's chat-image handling).
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30


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


def _content_url(base_url: str, workspace_id: str, feature_id: str, path: str) -> str:
    if not feature_id:
        # Workspace-root document — no owning feature (see module docstring).
        return f"{base_url}/api/workspaces/{workspace_id}/documents/content?path={quote(path, safe='')}"
    return f"{base_url}/api/workspaces/{workspace_id}/features/{feature_id}/documents/content?path={quote(path, safe='')}"


def _image_url(base_url: str, workspace_id: str, image_id: str) -> str:
    return f"{base_url}/api/workspaces/{workspace_id}/images/{quote(image_id, safe='')}"


def _create_document(
    base_url: str,
    headers: Dict[str, str],
    workspace_id: str,
    feature_id: str,
    path: str,
    feature_slug: str = "",
) -> None:
    """Create an empty document row via POST /api/documents.

    storage-service's content PUT is edit-only — it 404s ("document not
    found") for any path that has never been created, feature-scoped or
    workspace-root alike (only the three canonical docs are pre-created at
    feature-creation time). This creates the (empty) row so a follow-up PUT
    to the same path can then succeed. feature_id="" creates it at the
    workspace root, matching PutDocumentContent's own no-feature variant.

    feature_slug, when known, is stored alongside the row so storage-service
    builds the document's readable path/object key as
    "docs/features/{feature_slug}/{path}" instead of falling back to the raw
    feature_id (see FeatureRelativePath in storage-service's objectkey.go).

    Raises StorageServiceError on a non-2xx response (including "already
    exists" races, which the caller's retried PUT will simply overwrite).
    """
    url = f"{base_url}/api/documents"
    payload = {
        "workspace_id": workspace_id,
        "feature_id": feature_id,
        "path": path,
        "feature_slug": feature_slug,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=_DEFAULT_TIMEOUT)
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
            f"storage-service POST {url} returned {resp.status_code}: {body}",
            reason_code=reason,
            status=resp.status_code,
        )


def download_image(
    workspace_id: str,
    image_id: str,
    *,
    user_id: str = "",
    org_id: str = "",
) -> Dict[str, Any]:
    """Download a previously-uploaded image's raw bytes from storage-service.

    Used to fetch a chat-attached image server-side (trusted first-party
    code) so it can be handed to vision_analyze_tool as a local file path,
    rather than a URL — storage-service is an internal-only service, so a
    URL pointing at it would be rejected by vision_analyze_tool's SSRF guard
    (tools/url_safety.py) if the agent tried to fetch it directly as a tool
    call.

    Returns a dict with keys:
      - ``data`` (bytes): the raw image bytes
      - ``content_type`` (str): e.g. "image/png"

    Raises StorageServiceError on config errors, 404s, or other non-2xx responses.
    """
    base_url, token = _resolve_config()
    url = _image_url(base_url, workspace_id, image_id)
    headers = _build_headers(token, user_id, org_id)
    del headers["Content-Type"]  # GET; avoid implying a JSON body
    try:
        resp = requests.get(url, headers=headers, timeout=_DEFAULT_TIMEOUT)
    except requests.RequestException as exc:
        raise StorageServiceError(f"storage-service request failed: {exc}", reason_code="request_error") from exc

    if not resp.ok:
        reason = "not_found" if resp.status_code == 404 else ""
        raise StorageServiceError(
            f"storage-service GET {url} returned {resp.status_code}",
            reason_code=reason,
            status=resp.status_code,
        )

    return {
        "data": resp.content,
        "content_type": resp.headers.get("Content-Type", "application/octet-stream"),
    }


def read_document_content(
    workspace_id: str,
    feature_id: str,
    path: str,
    *,
    user_id: str = "",
    org_id: str = "",
) -> Dict[str, Any]:
    """Read a document's content from storage-service.

    Pass feature_id="" to read a workspace-root document (no owning
    feature) — path is then relative to the workspace root instead of a
    feature folder; see _content_url.

    Returns a dict with keys:
      - ``content`` (str): document markdown content, or ``""`` when not found
      - ``version_id`` (str | None): current version id

    Raises StorageServiceError on config errors or non-2xx/404 responses.
    """
    base_url, token = _resolve_config()
    url = _content_url(base_url, workspace_id, feature_id, path)
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
    path: str,
    content: str,
    *,
    user_id: str = "",
    org_id: str = "",
    feature_slug: str = "",
) -> Dict[str, Any]:
    """Write a document's content to storage-service.

    The content PUT is edit-only (see module docstring / _create_document) —
    a 404 on the first attempt means the document row doesn't exist yet, so
    this creates it via POST /api/documents and retries the PUT once. This
    covers any non-canonical path (feature-scoped or workspace-root) on its
    first write, not just the three pre-provisioned canonical documents.

    feature_slug, when known, is passed through to _create_document so a
    brand-new document lands under the feature's human-readable folder
    instead of a raw-UUID one. Only matters on first write (the 404 branch);
    an existing row already has its slug recorded.

    Returns a dict with keys:
      - ``ok`` (bool): True on success
      - ``version_id`` (str | None): new version id

    Raises StorageServiceError on config errors or non-2xx responses.
    """
    base_url, token = _resolve_config()
    url = _content_url(base_url, workspace_id, feature_id, path)
    headers = _build_headers(token, user_id, org_id)
    payload = {"content": content}

    def _put() -> requests.Response:
        try:
            return requests.put(url, headers=headers, json=payload, timeout=_DEFAULT_TIMEOUT)
        except requests.RequestException as exc:
            raise StorageServiceError(f"storage-service request failed: {exc}", reason_code="request_error") from exc

    resp = _put()
    if resp.status_code == 404:
        _create_document(base_url, headers, workspace_id, feature_id, path, feature_slug)
        resp = _put()

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
