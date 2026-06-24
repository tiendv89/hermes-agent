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
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

from ..db import _validate_id, get_feature_detail, get_workspace_context
from ..document_repo import read_document
from .artifacts import _resolve_document_branch, _resolve_management_repo

logger = logging.getLogger(__name__)

_DOCUMENT_FILES: Dict[str, str] = {
    "product_spec": "product-spec.md",
    "technical_design": "technical-design.md",
    "status": "status.yaml",
}

READ_DOCUMENT_SCHEMA: Dict[str, Any] = {
    "description": (
        "Read a feature document (product_spec, technical_design, or status) straight from the "
        "management repo's feature branch in git. Use this FIRST when writing or revising a "
        "technical design: call read_document(document='product_spec') to load the approved "
        "product spec and ground the design in its actual scope — do NOT infer the spec from RAG "
        "or the request text. This reads the feature branch directly, so it works even when the "
        "spec is unmerged and not yet indexed by RAG. Returns the document content and whether it "
        "exists."
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
                "enum": list(_DOCUMENT_FILES),
                "description": "Which document to read: 'product_spec', 'technical_design', or 'status'.",
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
    """Read a feature document from the resolved feature branch in git."""
    from ..context import get_feature_id, get_workspace_id, mark_context_gathered

    wid = workspace_id or get_workspace_id()
    fid = feature_id or get_feature_id()
    if not wid or not fid:
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }

    filename = _DOCUMENT_FILES.get(document)
    if filename is None:
        return {"ok": False, "error": f"Unknown document type: {document!r}. Must be one of {list(_DOCUMENT_FILES)}."}

    try:
        _validate_id(fid, "feature_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    try:
        workspace_context = get_workspace_context(wid)
        owner, repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        return {"ok": False, "error": f"Could not resolve management repo: {exc}"}

    base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")

    # All git artifacts are slug-keyed (branch feature/{slug}, path
    # docs/features/{slug}/...). Resolve the slug + the branch documents live on.
    slug = fid
    init_pr_url = None
    try:
        detail = get_feature_detail(wid, fid)
        slug = detail.get("feature_name") or fid
        init_pr_url = detail.get("init_pr_url")
    except Exception as exc:
        logger.debug("read_document: could not fetch feature_detail: %s", exc)

    target_branch, _pr_url = _resolve_document_branch(
        owner, repo, fid, slug, init_pr_url, base_branch, github_token
    )
    path = f"docs/features/{slug}/{filename}"

    try:
        current = read_document(owner, repo, target_branch, path, github_token)
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
