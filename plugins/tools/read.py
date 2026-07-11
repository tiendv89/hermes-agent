"""read_document — read a feature document from the management repo via git.

The design/spec authoring loop writes documents to the feature branch
(``feature/{slug}`` or ``feature/{slug}-init``) in the management repo. Those
documents are NOT reliably available through RAG — a feature branch is usually
unindexed (RAG only sees the default branch) and RAG never indexes prose specs
on unmerged branches. Without a way to read them, the agent guesses the feature
scope and produces an irrelevant design.

This tool exposes the same git-backed read path that ``edit_document`` and the
stage-transition endpoint already use (``document_repo.read_document`` against
the resolved feature branch), so the agent can load the actual
``product-spec.md`` / ``technical-design.md`` / ``status.yaml`` before drafting.

Owner guard: for go-owned features, document content lives in storage-service
(not git) and status lives in workflow-backend's DB. In that case, this tool
proxies to storage-service's document-content endpoint (using
STORAGE_SERVICE_TOKEN) for product_spec/technical_design/tasks, and to
get_feature_detail for status — no git access at all. ts-owned and
absent-owner features continue to use the git-backed path unchanged.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

import yaml

from ..document_repo import read_document
from ..storage_service_client import StorageServiceError, read_document_content
from ..validation import _validate_id
from .artifacts import _resolve_document_branch, _resolve_management_repo
from src.services.workflow_backend_client import get_feature_detail, get_workspace_context, run_async

logger = logging.getLogger(__name__)

_DOCUMENT_FILES: Dict[str, str] = {
    "product_spec": "product-spec.md",
    "technical_design": "technical-design.md",
    "status": "status.yaml",
}

# storage-service document paths for go-owned features — distinct from
# _DOCUMENT_FILES (git filenames, hyphenated) since storage-service documents
# are identified by path directly (see storage_service_client.py's contract),
# matching workflow-backend's provisioning convention (feature_create.go).
_STORAGE_DOC_PATHS: Dict[str, str] = {
    "product_spec": "product_spec.md",
    "technical_design": "tech_design.md",
}

READ_DOCUMENT_SCHEMA: Dict[str, Any] = {
    "description": (
        "Read a feature document straight from the management repo's feature branch in git (or, "
        "for go-owned features, from storage-service). Use this FIRST when writing or revising a "
        "technical design: call read_document(document='product_spec') to load the approved "
        "product spec and ground the design in its actual scope — do NOT infer the spec from RAG "
        "or the request text. This reads the feature branch directly, so it works even when the "
        "spec is unmerged and not yet indexed by RAG. Pass 'product_spec', 'technical_design', or "
        "'status' for the three canonical documents, or any other filename (e.g. 'README.md', "
        "'handoffs/handoff.md') to read an arbitrary file from the feature's document folder. "
        "Returns the document content and whether it exists."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier. Omit to use the current workspace from context.",
            },
            "feature_id": {
                "type": "string",
                "description": "Feature identifier. Omit to use the current feature from context.",
            },
            "document": {
                "type": "string",
                "description": (
                    "Which document to read. Use 'product_spec', 'technical_design', or 'status' "
                    "for the three canonical documents, or pass any other filename (e.g. "
                    "'README.md', 'handoffs/handoff.md') to read an arbitrary file from the "
                    "feature's document folder."
                ),
            },
        },
        "required": ["document"],
        "additionalProperties": False,
    },
}


def handle_read_document(
    document: str,
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> Dict[str, Any]:
    """Read a feature document from the resolved feature branch in git.

    For go-owned features, proxies to storage-service instead of git.
    """
    from ..context import get_feature_id, get_org_id, get_user_id, get_workspace_id, mark_context_gathered

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
    # Capture identity on this (calling) thread — run_async may bridge onto a
    # different thread, where thread-local context is unset.
    caller_user_id = get_user_id()
    caller_org_id = get_org_id()
    if not wid or not fid:
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }

    if not document.strip():
        return {"ok": False, "error": "document is required."}

    # A canonical name (product_spec/technical_design/status) maps to its
    # known git filename; anything else is treated as a literal filename
    # (e.g. "README.md", "handoffs/handoff.md") within the feature's document
    # folder — see _STORAGE_DOC_PATHS below for the equivalent go-owned
    # fallback.
    filename = _DOCUMENT_FILES.get(document, document)

    try:
        _validate_id(fid, "feature_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # Resolve owner to decide which backend to read from.
    owner_value: str = "ts"
    slug = fid
    init_pr_url = None
    try:
        detail = run_async(get_feature_detail(wid, fid, user_id=caller_user_id, org_id=caller_org_id))
        slug = detail.get("feature_name") or fid
        init_pr_url = detail.get("init_pr_url")
        owner_value = detail.get("owner") or "ts"
    except Exception as exc:
        logger.debug("read_document: could not fetch feature_detail: %s", exc)

    # Owner guard: go-owned features proxy to storage-service; ts/absent use git.
    if owner_value == "go":
        if document == "status":
            # go-owned features track status entirely in workflow-backend's DB —
            # there is no git status.yaml for them (workflow-backend no longer
            # creates an init branch/PR at feature creation for go). Synthesize
            # status.yaml-shaped content from the already-fetched feature detail.
            status_data = {
                "feature_status": detail.get("status") or "",
                "current_stage": detail.get("stage") or "",
                "next_action": detail.get("next_action") or "",
                "stages": dict(detail.get("stages") or {}),
            }
            content = yaml.dump(status_data, default_flow_style=False, allow_unicode=True)
            mark_context_gathered(fid)
            return {
                "ok": True,
                "exists": True,
                "document": document,
                "path": f"workflow-backend://{wid}/{fid}/status",
                "branch": None,
                "content": content,
                "sha": None,
            }

        storage_path = _STORAGE_DOC_PATHS.get(document, document)
        try:
            result = read_document_content(
                wid, fid, storage_path,
                user_id=caller_user_id, org_id=caller_org_id,
            )
        except StorageServiceError as exc:
            logger.warning("read_document (go): storage-service error: %s", exc)
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.warning("read_document (go): unexpected error: %s", exc)
            return {"ok": False, "error": str(exc)}

        content = result.get("content", "")
        exists = bool(content)
        if exists:
            mark_context_gathered(fid)
        return {
            "ok": True,
            "exists": exists,
            "document": document,
            "path": f"storage-service://{wid}/{fid}/{storage_path}",
            "branch": None,
            "content": content,
            "sha": result.get("version_id"),
        }

    # ts-owned (or absent owner, or status.yaml for go): read from git as before.
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    try:
        workspace_context = run_async(get_workspace_context(wid, user_id=caller_user_id, org_id=caller_org_id))
        gh_owner, gh_repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        return {"ok": False, "error": f"Could not resolve management repo: {exc}"}

    base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")

    target_branch, _pr_url = _resolve_document_branch(
        gh_owner, gh_repo, fid, slug, init_pr_url, base_branch, github_token
    )
    path = f"docs/features/{slug}/{filename}"

    try:
        current = read_document(gh_owner, gh_repo, target_branch, path, github_token)
    except Exception as exc:
        logger.warning("read_document failed: %s", exc)
        return {"ok": False, "error": str(exc)}

    exists = current.get("sha") is not None
    # Reading the spec is a legitimate context-gathering action; satisfy the
    # design-write gate so a grounded read isn't blocked behind RAG/GitNexus.
    if exists:
        mark_context_gathered(fid)

    return {
        "ok": True,
        "exists": exists,
        "document": document,
        "path": path,
        "branch": target_branch,
        "content": current.get("content", ""),
        "sha": current.get("sha"),
    }
