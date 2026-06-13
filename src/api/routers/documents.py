"""Human document-save route.

    PUT /features/{feature_id}/document — human-save a document to the feature branch
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.api.identity import Identity, require_identity

logger = logging.getLogger(__name__)

router = APIRouter()


class SaveDocumentRequest(BaseModel):
    doc: str  # "product_spec" or "technical_design"
    content: str
    base_sha: Optional[str] = None  # None for new files


@router.put("/features/{feature_id}/document")
async def save_document_endpoint(
    feature_id: str,
    body: SaveDocumentRequest,
    identity: Identity = Depends(require_identity),
) -> JSONResponse:
    """Commit a human-edited document to the feature branch.

    The ``base_sha`` comes from workflow-backend's document-content view API
    (the read-before-write optimistic lock). A 409 response means the document
    changed since the editor loaded it — the FE should reload and retry.
    """
    import os as _os

    from plugins.db import _validate_id, get_workspace_context
    from plugins.document_repo import StaleBaseError, write_document
    from plugins.tools.artifacts import _resolve_management_repo

    _DOCUMENT_FILES = {
        "product_spec": "product-spec.md",
        "technical_design": "technical-design.md",
    }

    filename = _DOCUMENT_FILES.get(body.doc)
    if filename is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown document type: {body.doc!r}. Must be one of {list(_DOCUMENT_FILES)}.",
        )

    try:
        _validate_id(feature_id, "feature_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    github_token = _os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        raise HTTPException(status_code=500, detail="GITHUB_TOKEN is not configured on the server.")

    # Workspace resolution: infer workspace from the feature context if needed.
    # For the human-save path we look up the workspace via the feature context
    # stored in the agent session. As a fallback we read WORKSPACE_ID from env.
    workspace_id = _os.environ.get("WORKSPACE_ID", "").strip()
    if not workspace_id:
        raise HTTPException(
            status_code=500,
            detail="WORKSPACE_ID is not configured — cannot resolve management repo.",
        )

    try:
        workspace_context = get_workspace_context(workspace_id)
        owner, repo = _resolve_management_repo(workspace_context)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not resolve management repo: {exc}",
        ) from exc

    base_branch = _os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")
    path = f"docs/features/{feature_id}/{filename}"
    commit_message = f"docs: human save {body.doc.replace('_', '-')} (via hermes)"

    try:
        result = write_document(
            owner, repo, feature_id, base_branch, path,
            body.content, body.base_sha, commit_message, github_token,
        )
    except StaleBaseError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "conflict": True,
                "message": "Document changed since you opened it. Reload and retry.",
                "detail": str(exc),
            },
        ) from exc
    except Exception as exc:
        logger.exception("save_document failed for feature %s", feature_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return JSONResponse({
        "ok": True,
        "commit_sha": result["commit_sha"],
        "pr": result["pr"],
    })
