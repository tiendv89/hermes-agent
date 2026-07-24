"""write_product_spec / write_technical_design tools.

Full-rewrite handlers: write document content to storage-service.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from plugins.clients.storage_service_client import (
    StorageServiceError,
    write_document_content,
)

from ..validation import _validate_id

logger = logging.getLogger(__name__)

# Canonical feature-doc templates the generated artifacts must follow. Bundled
# under plugins/skills/templates/feature/ (a copy of agent-workflow/templates).
_TEMPLATES_DIR = (
    Path(__file__).resolve().parent.parent / "skills" / "templates" / "feature"
)


def _load_template(filename: str) -> str:
    """Return a bundled feature-doc template, or '' if it can't be read."""
    try:
        return (_TEMPLATES_DIR / filename).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        logger.warning(
            "artifacts: template %s not found under %s", filename, _TEMPLATES_DIR
        )
        return ""


def _content_description(filename: str, template: str) -> str:
    """Build the ``content`` field description, embedding the template so the
    agent generates a document that matches its structure exactly."""
    desc = (
        f"Full Markdown content for {filename} — the complete document, not a diff. "
        f"Follow the template below exactly: keep every section heading and their "
        f"order, fill each section with real content, and replace the <...> "
        f"placeholders. You may add subsections within a section, but do not drop "
        f"or rename the template's headings."
    )
    if template:
        desc += f"\n\n--- TEMPLATE ({filename}) ---\n{template}\n--- END TEMPLATE ---"
    return desc


_PRODUCT_SPEC_TEMPLATE = _load_template("product-spec.md")
_TECHNICAL_DESIGN_TEMPLATE = _load_template("technical-design.md")

WRITE_SPEC_SCHEMA: dict[str, Any] = {
    "description": (
        "Write the feature's product-spec.md — stores the full Markdown to "
        "storage-service. Use when authoring or revising "
        "the product specification. The content must follow the product-spec "
        "template (see the content field); pass the complete document, not a diff. "
        "REQUIRED FIRST: gather repository context with query_rag and "
        "query_gitnexus (start with tool='list_repos') before calling this — "
        "ground the spec in real repos/tables/symbols, not assumptions. If both "
        "return nothing, note the unresolved technical questions in the document "
        "rather than inventing names. But for product decisions no tool can "
        "resolve — target users, success metrics, core scope/priority, "
        "trade-offs with real business impact — use the clarify tool to ask the "
        "user before writing, rather than guessing or silently noting the gap. "
        "(Interactive sessions only — skip clarify and fall back to noting the "
        "gap in the document when AGENT_RUNTIME=1, since there is no one to answer.) "
        "In a general-chat session (no current feature), "
        "pass the SAME feature_id to query_rag/query_gitnexus that you pass here "
        "— otherwise those calls credit no feature and this tool keeps reporting "
        "needs_context."
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
            "content": {
                "type": "string",
                "description": _content_description(
                    "product-spec.md", _PRODUCT_SPEC_TEMPLATE
                ),
            },
            "commit_message": {
                "type": "string",
                "description": "Git commit message (optional).",
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    },
}

WRITE_TD_SCHEMA: dict[str, Any] = {
    "description": (
        "Write the feature's technical-design.md — stores the full Markdown to "
        "storage-service. Use when authoring or "
        "revising the technical design. The content must follow the "
        "technical-design template (see the content field); pass the complete "
        "document, not a diff. REQUIRED FIRST: call read_file(document='product_spec') "
        "to load the approved spec from the feature branch and ground the design in its "
        "actual scope (it works even when the spec is unmerged/unindexed) — never infer "
        "scope from RAG or the request text. Then gather repository context with "
        "query_rag and query_gitnexus (start with tool='list_repos', then "
        "tool='query'/'context'/'impact' for the symbols the design touches) "
        "before calling this — ground the design in real repos/files/symbols, "
        "not assumptions. If both return nothing, note the unresolved repo/symbol "
        "questions in the document rather than inventing names. But for design "
        "decisions no tool can resolve — which of several viable approaches to "
        "take, a trade-off with real cost/risk implications — use the clarify "
        "tool to ask the user before writing, rather than guessing. (Interactive "
        "sessions only — skip clarify and fall back to noting the gap in the "
        "document when AGENT_RUNTIME=1, since there is no one to answer.) "
        "In a general-chat "
        "session (no current feature), pass the SAME feature_id to "
        "query_rag/query_gitnexus that you pass here — otherwise those calls "
        "credit no feature and this tool keeps reporting needs_context."
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
            "content": {
                "type": "string",
                "description": _content_description(
                    "technical-design.md", _TECHNICAL_DESIGN_TEMPLATE
                ),
            },
            "commit_message": {
                "type": "string",
                "description": "Git commit message (optional).",
            },
        },
        "required": ["content"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Internal write pipeline
# ---------------------------------------------------------------------------


def _coerce_content(content: Any) -> str:
    """Normalize tool ``content`` to a string before it is UTF-8 encoded.

    The tool schema declares ``content`` as a string, but over the MCP path the
    model occasionally passes a structured JSON object instead of markdown. That
    dict would otherwise reach ``str.encode`` in storage_service_client and raise
    ``'dict' object has no attribute 'encode'``.

    An empty dict/list (``{}``, ``[]``) is a model mistake — the model called
    the tool without generating actual content. We raise ValueError so the
    caller returns an error rather than writing useless bytes to storage-service.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        if not content:
            raise ValueError(
                "content is an empty object — the model did not generate document "
                "content before calling the tool. Regenerate the full markdown "
                "document and pass it as the content string."
            )
        # Model wrapped the markdown in {"content": "...", ...} — extract the string.
        if isinstance(content.get("content"), str):
            return content["content"]
        return json.dumps(content, indent=2, ensure_ascii=False)
    if isinstance(content, list):
        if not content:
            raise ValueError(
                "content is an empty list — the model did not generate document "
                "content before calling the tool. Regenerate the full markdown "
                "document and pass it as the content string."
            )
        return json.dumps(content, indent=2, ensure_ascii=False)
    return str(content)


def _write_artifact(
    workspace_id: str,
    feature_id: str,
    filename: str,
    content: str,
    commit_message: str,
    stage: str,
) -> dict[str, Any]:
    """Write document content to storage-service."""
    if not feature_id:
        return {
            "ok": False,
            "error": (
                "feature_id could not be resolved for the current session. "
                "Pass feature_id explicitly in the tool call, or ensure "
                "set_context() was called with this session's feature_id "
                "before invoking write tools."
            ),
        }
    _validate_id(feature_id, "feature_id")

    # Hard gate: a product spec / technical design must be grounded in the
    # indexed codebase. Block the write until the agent has gathered context via
    # query_rag or query_gitnexus for this feature in the session. Marking is set
    # by those tool handlers (see plugins/context.py); once set it persists, so
    # later revisions of the same doc are not re-blocked.
    from ..context import was_context_gathered

    if not was_context_gathered(feature_id):
        return {
            "ok": False,
            "needs_context": True,
            "error": (
                "Context not gathered. Before writing the "
                f"{stage.replace('_', ' ')}, you must ground it in the codebase: "
                "call query_rag for the feature's domain/entities, and "
                "query_gitnexus (tool='list_repos', then tool='query') for the "
                "relevant repos/symbols. Run at least one of them for this "
                "feature, then call this tool again. If both return nothing, that "
                "still satisfies this gate — record the unresolved questions in "
                "the document instead of inventing repo/table/symbol names."
            ),
        }

    try:
        content = _coerce_content(content)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    if not content.strip():
        return {
            "ok": False,
            "error": (
                "content is empty — the document was not written to GitHub. "
                "Generate the full markdown document and pass it as the content string."
            ),
        }

    from ..context import get_org_id, get_user_id

    caller_user_id = get_user_id()
    caller_org_id = get_org_id()

    # Resolve feature_id to its canonical business-key UUID before writing.
    # write_document_content keys the storage-service document by
    # (workspace_id, feature_id, path); a session/tool caller may hand us the
    # feature's slug instead of its UUID (both are accepted loosely upstream —
    # see workflow_backend_client.get_feature_detail's docstring), and writing
    # under the raw slug creates a SECOND document row at the same logical
    # path instead of updating the one workflow-backend already provisioned
    # under the UUID. Best-effort: on resolution failure, fall back to the
    # raw feature_id rather than hard-failing the write.
    slug = ""
    try:
        from src.services.workflow_backend_client import get_feature_detail, run_async

        detail = run_async(
            get_feature_detail(workspace_id, feature_id, user_id=caller_user_id, org_id=caller_org_id)
        )
        feature_id = detail.get("id") or feature_id
        slug = detail.get("feature_name") or ""
    except Exception as exc:
        logger.warning("_write_artifact: could not resolve feature_detail: %s", exc)

    storage_path_map = {
        "product_spec": "product_spec.md",
        "technical_design": "tech_design.md",
    }
    storage_path = storage_path_map.get(stage, stage)
    try:
        result = write_document_content(
            workspace_id,
            feature_id,
            storage_path,
            content,
            user_id=caller_user_id,
            org_id=caller_org_id,
            feature_slug=slug,
        )
    except StorageServiceError as exc:
        logger.warning("_write_artifact: storage-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("_write_artifact: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "path": f"storage-service://{workspace_id}/{feature_id}/{storage_path}",
        "commit": None,
        "commit_sha": None,
        "pr_url": None,
        "conflict": False,
        "version_id": result.get("version_id"),
    }


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _resolve_ids(workspace_id: str, feature_id: str) -> tuple[str, str]:
    from ..context import get_agent_session_id, get_context_for_session

    session_id = get_agent_session_id()
    ctx_workspace_id, ctx_feature_id = get_context_for_session(session_id)
    return workspace_id or ctx_workspace_id, feature_id or ctx_feature_id


def handle_write_product_spec(
    content: str,
    workspace_id: str = "",
    feature_id: str = "",
    commit_message: str = "docs: update product spec",
    **_: Any,
) -> dict[str, Any]:
    wid, fid = _resolve_ids(workspace_id, feature_id)
    if not wid or not fid:
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }
    try:
        return _write_artifact(
            wid, fid, "product-spec.md", content, commit_message, stage="product_spec"
        )
    except Exception as exc:
        logger.warning("write_product_spec failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def handle_write_technical_design(
    content: str,
    workspace_id: str = "",
    feature_id: str = "",
    commit_message: str = "docs: update technical design",
    **_: Any,
) -> dict[str, Any]:
    wid, fid = _resolve_ids(workspace_id, feature_id)
    if not wid or not fid:
        return {
            "ok": False,
            "error": "workspace_id and feature_id are required but were not provided and no context is set.",
        }
    try:
        return _write_artifact(
            wid,
            fid,
            "technical-design.md",
            content,
            commit_message,
            stage="technical_design",
        )
    except Exception as exc:
        logger.warning("write_technical_design failed: %s", exc)
        return {"ok": False, "error": str(exc)}
