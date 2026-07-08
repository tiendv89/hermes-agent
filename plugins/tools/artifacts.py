"""write_product_spec / write_technical_design tools.

Full-rewrite handlers: read the current document SHA, replace the entire
content, and commit to the feature branch via the document_repo pipeline.
This replaces the old direct-to-main Contents PUT and fixes the
"no direct push to main" rule violation.

Owner guard: for go-owned features, document content lives in storage-service
(not git). write_product_spec / write_technical_design proxy to storage-service
using STORAGE_SERVICE_TOKEN. ts-owned and absent-owner features continue to
commit to git unchanged.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


from ..validation import _validate_id
from ..document_repo import (
    StaleBaseError,
    branch_exists,
    commit_files,
    commit_to_branch,
    ensure_branch,
    ensure_feature_branch,
    ensure_pr_for_head,
    read_document,
    write_document,
)
from ..storage_service_client import StorageServiceError, write_document_content

import yaml as _yaml

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_GITHUB_API_URL = "https://api.github.com"
_GITHUB_SSH_RE = re.compile(r"git@github\.com:([^/]+)/([^\.]+?)(?:\.git)?$")
_GITHUB_HTTPS_RE = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?$")

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

WRITE_SPEC_SCHEMA: Dict[str, Any] = {
    "description": (
        "Write the feature's product-spec.md — commits the full Markdown to the "
        "feature branch (opening/updating its PR). Use when authoring or revising "
        "the product specification. The content must follow the product-spec "
        "template (see the content field); pass the complete document, not a diff. "
        "REQUIRED FIRST: gather repository context with query_rag and "
        "query_gitnexus (start with tool='list_repos') before calling this — "
        "ground the spec in real repos/tables/symbols, not assumptions. If both "
        "return nothing, note the unresolved questions in the document rather "
        "than inventing names."
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

WRITE_TD_SCHEMA: Dict[str, Any] = {
    "description": (
        "Write the feature's technical-design.md — commits the full Markdown to "
        "the feature branch (opening/updating its PR). Use when authoring or "
        "revising the technical design. The content must follow the "
        "technical-design template (see the content field); pass the complete "
        "document, not a diff. REQUIRED FIRST: call read_document(document='product_spec') "
        "to load the approved spec from the feature branch and ground the design in its "
        "actual scope (it works even when the spec is unmerged/unindexed) — never infer "
        "scope from RAG or the request text. Then gather repository context with "
        "query_rag and query_gitnexus (start with tool='list_repos', then "
        "tool='query'/'context'/'impact' for the symbols the design touches) "
        "before calling this — ground the design in real repos/files/symbols, "
        "not assumptions. If both return nothing, note the unresolved repo/symbol "
        "questions in the document rather than inventing names."
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
# GitHub helpers — kept for backward compat and use in document_repo resolution
# ---------------------------------------------------------------------------


def _parse_github_owner_repo(github_url: str) -> Tuple[str, str]:
    m = _GITHUB_SSH_RE.match(github_url.strip()) or _GITHUB_HTTPS_RE.match(
        github_url.strip()
    )
    if m:
        return m.group(1), m.group(2)
    raise ValueError(f"Cannot parse GitHub owner/repo from URL: {github_url!r}")


def _management_repo_from_env() -> Optional[Tuple[str, str]]:
    """Env fallback for the management repo when the workspace context has no
    github source configured. Accepts a git URL or a plain ``owner/repo``.

    Set ``MANAGEMENT_REPO_GITHUB`` to unblock document writes when the
    workflow-backend DB has no ``workspace_github_sources.repo_url`` row yet.
    """
    env_repo = os.environ.get("MANAGEMENT_REPO_GITHUB", "").strip()
    if not env_repo:
        return None
    try:
        return _parse_github_owner_repo(env_repo)
    except ValueError:
        owner, _, repo = env_repo.partition("/")
        repo = repo[:-4] if repo.endswith(".git") else repo
        if owner and repo and "/" not in repo:
            return owner, repo
    return None


def _resolve_management_repo(workspace_context: Dict[str, Any]) -> Tuple[str, str]:
    management_repo_id: Optional[str] = workspace_context.get("management_repo")
    repos: list = workspace_context.get("repos", [])

    if management_repo_id:
        for repo in repos:
            if isinstance(repo, dict) and repo.get("id") == management_repo_id:
                if repo.get("github"):
                    return _parse_github_owner_repo(repo["github"])

    for repo in repos:
        if (
            isinstance(repo, dict)
            and "management" in repo.get("id", "")
            and repo.get("github")
        ):
            return _parse_github_owner_repo(repo["github"])

    env_fallback = _management_repo_from_env()
    if env_fallback is not None:
        return env_fallback

    raise ValueError(
        f"Could not resolve management repo: the workspace has no github source "
        f"configured (management_repo={management_repo_id!r}, repos={repos!r}). "
        f"Configure the workspace's repo in the workflow-backend, or set "
        f"MANAGEMENT_REPO_GITHUB (a git URL or 'owner/repo') to override."
    )


# ---------------------------------------------------------------------------
# Internal write pipeline
# ---------------------------------------------------------------------------


def _coerce_content(content: Any) -> str:
    """Normalize tool ``content`` to a string before it is UTF-8 encoded.

    The tool schema declares ``content`` as a string, but over the MCP path the
    model occasionally passes a structured JSON object instead of markdown. That
    dict would otherwise reach ``str.encode`` in document_repo and raise
    ``'dict' object has no attribute 'encode'``.

    An empty dict/list (``{}``, ``[]``) is a model mistake — the model called
    the tool without generating actual content. We raise ValueError so the
    caller returns an error rather than committing useless bytes to GitHub.
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


def _owner_guard_ts_only(owner: Optional[str]) -> Optional[Dict[str, Any]]:
    """Return a skip response for go features; return None to allow ts/absent features to proceed.

    Applied before operations that are only valid for TypeScript/git (ts) features,
    such as writing task YAML files or creating task-state git branches.
    Document writes (product-spec, technical-design) are NOT blocked — use branch
    decision logic there instead.
    """
    if owner == "go":
        return {
            "ok": False,
            "skipped": True,
            "reason": (
                "This operation is only available for TypeScript/git (ts) features. "
                "The current feature uses the Postgres/Go (go) orchestrator — "
                "task YAML files and task-state branches are managed by the database, "
                "not by git."
            ),
        }
    return None


def _resolve_document_branch(
    gh_owner: str,
    gh_repo: str,
    feature_id: str,
    feature_name: Optional[str],
    init_pr_url: Optional[str],
    base_branch: str,
    github_token: str,
) -> Tuple[str, Optional[str]]:
    """Return (target_branch, known_pr_url) for the document write.

    Decision logic (backward-compatible):
    - init_pr_url non-null + init branch exists  → commit to feature/<slug>-init;
                                                    return (init branch, init_pr_url)
    - init_pr_url non-null + branch gone (merged) → commit to feature/<slug>;
                                                    return (feature branch, None)
    - init_pr_url null (pre-existing feature)     → commit to feature/<slug> directly;
                                                    return (feature branch, None)

    All git branches use the feature *slug* (feature_name), never the UUID:
    workflow-backend creates ``feature/{slug}-init`` and that init branch is the
    canonical design-phase authoring branch — every product-spec / tech-design /
    tasks / status write lands there (one branch, one PR) until the init PR is
    merged. Everything written to git is slug-keyed.
    """
    # Everything in git is slug-keyed. Fall back to feature_id only when the
    # slug couldn't be resolved (the model may already have passed a slug).
    slug = feature_name or feature_id
    init_branch = f"feature/{slug}-init"
    feature_branch = f"feature/{slug}"

    # Prefer the init branch whenever it exists — it is the canonical design
    # branch, independent of whether init_pr_url was persisted in the DB.
    if branch_exists(gh_owner, gh_repo, init_branch, github_token):
        return init_branch, init_pr_url
    # Branch absent but the feature still carries an init PR — recreate it from
    # base so design docs stay on the one init branch instead of spawning a
    # separate feature/{slug} branch + PR.
    if init_pr_url:
        ensure_branch(gh_owner, gh_repo, init_branch, base_branch, github_token)
        return init_branch, init_pr_url

    # Legacy feature with no init branch and no init PR — use the feature branch.
    ensure_feature_branch(gh_owner, gh_repo, slug, base_branch, github_token)
    return feature_branch, None


def _status_stage_block() -> Dict[str, Any]:
    return {
        "review_status": "draft",
        "reviewed_by": None,
        "reviewed_at": None,
        "review_comment": None,
        "review_history": [],
    }


def _build_status_yaml(slug: str, title: str, owner: str) -> str:
    """Build a fresh status.yaml mirroring workflow-backend's feature scaffold."""
    stages = {
        "product_spec": _status_stage_block(),
        "technical_design": _status_stage_block(),
        "tasks": _status_stage_block(),
        "handoff": _status_stage_block(),
    }
    data: Dict[str, Any] = {
        "feature_id": slug,
        "title": title or slug,
    }
    if owner == "go":
        data["owner"] = "go"
        del stages["tasks"]  # go features manage tasks in the DB, not git
    data["feature_status"] = "in_design"
    data["current_stage"] = "product_spec"
    data["stages"] = stages
    data["history"] = []
    data["revalidation"] = {
        "product_spec_required": False,
        "technical_design_required": False,
        "tasks_required": False,
        "deployment_checklist_required": False,
    }
    return _yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)


_TD_TEMPLATE = (
    "# Technical Design\n\n## Feature\n- Feature ID: `{slug}`\n- Title: {title}\n\n"
    "## Current State\n\n_Describe the current state._\n\n"
    "## Chosen Design\n\n_Describe the chosen design._\n\n"
    "## Dependency Analysis\n\n_Describe dependencies._\n\n"
    "## Validation and Release Impact\n\n_Describe testing expectations and rollout concerns._\n"
)


def _ensure_feature_scaffold(
    gh_owner: str,
    gh_repo: str,
    branch: str,
    slug: str,
    title: str,
    owner: str,
    github_token: str,
) -> None:
    """Ensure the feature scaffold exists on *branch*.

    workflow-backend only scaffolds status.yaml + templates on the init branch
    at feature creation. If that branch was deleted (or recreated from main,
    which never received the scaffold), a doc write would land on a branch with
    no status.yaml — breaking approve_feature / stage transitions / the UI.

    When status.yaml is absent, commit it (plus the technical-design template
    and the handoffs/tasks .gitkeep dirs) in one commit so the feature is
    well-formed regardless of how the branch came to be. No-ops when present.
    """
    base = f"docs/features/{slug}/"
    existing = read_document(gh_owner, gh_repo, branch, base + "status.yaml", github_token)
    if existing.get("content"):
        return  # already scaffolded

    files: Dict[str, str] = {
        base + "status.yaml": _build_status_yaml(slug, title, owner),
        base + "handoffs/.gitkeep": "",
    }
    # Only add technical-design.md if absent — never clobber an agent-written one.
    td = read_document(gh_owner, gh_repo, branch, base + "technical-design.md", github_token)
    if not td.get("content"):
        files[base + "technical-design.md"] = _TD_TEMPLATE.format(slug=slug, title=title or slug)
    if owner != "go":
        files[base + "tasks/.gitkeep"] = ""

    commit_files(
        gh_owner, gh_repo, branch, files,
        f"chore({slug}): scaffold feature (status.yaml + templates)", github_token,
    )


def _write_artifact(
    workspace_id: str,
    feature_id: str,
    filename: str,
    content: str,
    commit_message: str,
    stage: str,
) -> Dict[str, Any]:
    """Full-rewrite path: determine target branch, write document, call request_approval."""
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
    from src.services.workflow_backend_client import get_feature_detail, get_workspace_context, run_async

    # Capture identity on this (calling) thread — run_async may bridge onto a
    # different thread, where thread-local context is unset.
    caller_user_id = get_user_id()
    caller_org_id = get_org_id()

    # Look up init_pr_url, feature_name, title and owner from the DB first so we
    # can branch on owner before touching GitHub. go-owned features do not need
    # GITHUB_TOKEN or the workspace management-repo resolution.
    init_pr_url: Optional[str] = None
    feature_name: Optional[str] = None
    feature_title: str = ""
    owner: str = "ts"
    try:
        feature_detail = run_async(get_feature_detail(workspace_id, feature_id, user_id=caller_user_id, org_id=caller_org_id))
        init_pr_url = feature_detail.get("init_pr_url")
        feature_name = feature_detail.get("feature_name")
        feature_title = feature_detail.get("title") or ""
        owner = feature_detail.get("owner") or "ts"
    except Exception as exc:
        logger.debug("_write_artifact: could not fetch feature_detail: %s", exc)

    # Owner guard: go-owned features store document content in storage-service,
    # not git. Proxy the write there and return early — no git commit.
    if owner == "go":
        # Derive the document kind from the stage/filename mapping.
        kind_map = {"product_spec": "product_spec", "technical_design": "technical_design"}
        kind = kind_map.get(stage, stage)
        try:
            result = write_document_content(
                workspace_id, feature_id, kind, content,
                user_id=caller_user_id, org_id=caller_org_id,
            )
        except StorageServiceError as exc:
            logger.warning("_write_artifact (go): storage-service error: %s", exc)
            return {"ok": False, "error": str(exc)}
        except Exception as exc:
            logger.warning("_write_artifact (go): unexpected error: %s", exc)
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "path": f"storage-service://{workspace_id}/{feature_id}/{kind}",
            "commit": None,
            "commit_sha": None,
            "pr_url": None,
            "conflict": False,
            "version_id": result.get("version_id"),
        }

    # ts path: GITHUB_TOKEN and workspace management-repo are required from here.
    github_token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not github_token:
        return {"ok": False, "error": "GITHUB_TOKEN is not set in the environment."}

    workspace_context = run_async(get_workspace_context(workspace_id, user_id=caller_user_id, org_id=caller_org_id))
    gh_owner, gh_repo = _resolve_management_repo(workspace_context)
    base_branch = os.environ.get("MANAGEMENT_REPO_BASE_BRANCH", "main")

    # All git artifacts are slug-keyed (branch + docs path). Fall back to the
    # passed id only when the slug couldn't be resolved from the DB.
    slug = feature_name or feature_id

    target_branch, known_pr_url = _resolve_document_branch(
        gh_owner, gh_repo, feature_id, feature_name, init_pr_url, base_branch, github_token
    )

    # Ensure the feature scaffold (status.yaml + templates) exists on the target
    # branch — it may be missing if the init branch was deleted or recreated
    # from main. status.yaml is required by approve_feature / stage transitions.
    try:
        _ensure_feature_scaffold(gh_owner, gh_repo, target_branch, slug, feature_title, owner, github_token)
    except Exception as exc:
        logger.warning("_write_artifact: scaffold ensure failed (continuing): %s", exc)

    # docs/features/{slug}/... — workflow-backend scaffolds docs under the slug.
    path = f"docs/features/{slug}/{filename}"

    # Read-before-write: fetch the current SHA so GitHub accepts our PUT.
    current = read_document(gh_owner, gh_repo, target_branch, path, github_token)

    if target_branch.endswith("-init"):
        # Init branch path: commit directly, then ensure a single open init PR
        # exists (reuse the known one, else find/create it) — never spawn a
        # separate feature/{slug} PR while the feature is still in design.
        commit_sha = commit_to_branch(
            gh_owner,
            gh_repo,
            target_branch,
            path,
            content,
            current["sha"],
            commit_message,
            github_token,
        )
        pr = ensure_pr_for_head(
            gh_owner,
            gh_repo,
            target_branch,
            base_branch,
            github_token,
            title=f"docs({slug}): product spec + technical design",
            body=f"Design-phase authoring PR for feature `{slug}` (hermes-agent).",
        )
        pr_url = pr.get("url", "") or (known_pr_url or "")
    else:
        # Feature branch path: write via the standard pipeline (ensures PR exists).
        # Pass the slug so write_document commits to feature/{slug}.
        result = write_document(
            gh_owner,
            gh_repo,
            slug,
            base_branch,
            path,
            content,
            current["sha"],
            commit_message,
            github_token,
        )
        commit_sha = result["commit_sha"]
        pr_url = result["pr"].get("url", "")

    # Call request_approval so the approve button appears in the feature detail view.
    approval_request: Optional[Dict[str, Any]] = None
    try:
        from .approval import handle as _request_approval

        approval_result = _request_approval(stage=stage, feature_id=feature_id)
        if approval_result.get("ok"):
            approval_request = approval_result.get("approval_request")
    except Exception as exc:
        logger.warning("_write_artifact: request_approval failed: %s", exc)

    out: Dict[str, Any] = {
        "ok": True,
        "path": path,
        "commit": commit_sha,
        "commit_sha": commit_sha,
        "pr_url": pr_url,
        "conflict": False,
    }
    if approval_request:
        out["approval_request"] = approval_request
    return out


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


def _resolve_ids(workspace_id: str, feature_id: str) -> tuple[str, str]:
    from ..context import get_feature_id, get_workspace_id

    return workspace_id or get_workspace_id(), feature_id or get_feature_id()


def handle_write_product_spec(
    content: str,
    workspace_id: str = "",
    feature_id: str = "",
    commit_message: str = "docs: update product spec",
    **_: Any,
) -> Dict[str, Any]:
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
    except StaleBaseError as exc:
        return {"ok": False, "conflict": True, "error": str(exc)}
    except Exception as exc:
        logger.warning("write_product_spec failed: %s", exc)
        return {"ok": False, "error": str(exc)}


def handle_write_technical_design(
    content: str,
    workspace_id: str = "",
    feature_id: str = "",
    commit_message: str = "docs: update technical design",
    **_: Any,
) -> Dict[str, Any]:
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
    except StaleBaseError as exc:
        return {"ok": False, "conflict": True, "error": str(exc)}
    except Exception as exc:
        logger.warning("write_technical_design failed: %s", exc)
        return {"ok": False, "error": str(exc)}
