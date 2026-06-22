"""pre_llm_call hook — returns workspace/feature context for injection into the user message."""

from __future__ import annotations

import logging
import os
from typing import Any

from .context import get_context_for_session
from .db import check_workflow_available
from .skills import get_index

logger = logging.getLogger(__name__)

# Repo-ID fragments → knowledge skill names (heuristic stack matching for G10).
# Matches on substrings of the repo id (case-insensitive).
_STACK_SKILL_MAP: list[tuple[str, list[str]]] = [
    ("ui",          ["typescript-best-practices", "nextjs-best-practices", "heroui-react", "frontend-engineer"]),
    ("frontend",    ["typescript-best-practices", "nextjs-best-practices", "frontend-engineer"]),
    ("web",         ["typescript-best-practices", "nextjs-best-practices", "frontend-engineer"]),
    ("next",        ["nextjs-best-practices", "typescript-best-practices"]),
    ("react",       ["heroui-react", "typescript-best-practices", "frontend-engineer"]),
    ("mobile",      ["react-native-mobile-engineer", "typescript-best-practices"]),
    ("backend",     ["backend-engineer"]),
    ("api",         ["backend-engineer"]),
    ("service",     ["backend-engineer"]),
    ("hermes",      ["python-best-practices", "backend-engineer"]),
    ("workflow",    ["go-best-practices", "backend-engineer"]),
    ("go",          ["go-best-practices"]),
    ("python",      ["python-best-practices"]),
    ("data",        ["python-data", "data-engineer"]),
    ("postgres",    ["postgres-best-practices"]),
    ("nestjs",      ["nestjs-best-practices", "typescript-best-practices"]),
    ("directus",    ["directus-vue-engineer", "typescript-best-practices"]),
]


def _skills_for_repos(repo_ids: list[str]) -> list[str]:
    """Return an ordered, deduplicated list of skill names that match the given repo IDs."""
    seen: set[str] = set()
    result: list[str] = []
    for repo_id in repo_ids:
        lower = repo_id.lower()
        for fragment, skills in _STACK_SKILL_MAP:
            if fragment in lower:
                for s in skills:
                    if s not in seen:
                        seen.add(s)
                        result.append(s)
    return result


def _build_skills_block(feature_id: str, feature_stage: str, repo_ids: list[str]) -> str | None:
    """Build the skills injection block.

    For the technical-design stage, surface stack-matched technical_skills
    prominently (G10). For all stages, list available skills.
    """
    index = get_index()
    if not index:
        return None

    knowledge = {name: e for name, e in index.items() if not e.is_authoring}
    authoring = {name: e for name, e in index.items() if e.is_authoring}

    lines: list[str] = ["## Available skills (call load_skill to load full content)"]

    # For the technical-design stage, surface matched technical_skills first.
    if feature_stage == "technical_design" and repo_ids:
        matched_names = _skills_for_repos(repo_ids)
        matched = [(n, knowledge[n]) for n in matched_names if n in knowledge]
        if matched:
            lines.append("\n### Stack-matched skills for this feature's repos (load these first)")
            for name, entry in matched:
                lines.append(f"  {name}: {entry.description}")

        remaining = [(n, e) for n, e in sorted(knowledge.items()) if n not in {m[0] for m in matched}]
        if remaining:
            lines.append("\n### Other knowledge skills")
            for name, entry in remaining:
                lines.append(f"  {name}: {entry.description}")
    else:
        if knowledge:
            lines.append("\n### Knowledge skills")
            for name, entry in sorted(knowledge.items()):
                lines.append(f"  {name}: {entry.description}")

    if authoring:
        lines.append("\n### Authoring skills (workflow guidance)")
        for name, entry in sorted(authoring.items()):
            lines.append(f"  {name}: {entry.description}")

    return "\n".join(lines)


def _read_init_pr_context(
    feature_name: str,
    init_pr_url: str,
    gh_owner: str,
    gh_repo: str,
    github_token: str,
) -> str | None:
    """Read status.yaml + existing docs from the init branch and return a formatted block.

    Called only when the feature has an active init PR so the agent knows the
    current scaffold state before writing product-spec, technical-design, or
    calling approve_feature.
    """
    try:
        from .document_repo import branch_exists, read_document
    except Exception:
        return None

    init_branch = f"feature/{feature_name}-init"
    if not branch_exists(gh_owner, gh_repo, init_branch, github_token):
        return None

    lines = [f"## Init PR context (branch: {init_branch}, PR: {init_pr_url})"]

    doc_files = [
        ("status.yaml", f"docs/features/{feature_name}/status.yaml"),
        ("product-spec.md", f"docs/features/{feature_name}/product-spec.md"),
        ("technical-design.md", f"docs/features/{feature_name}/technical-design.md"),
    ]

    any_content = False
    for label, path in doc_files:
        try:
            result = read_document(gh_owner, gh_repo, init_branch, path, github_token)
            content = (result.get("content") or "").strip()
            if content:
                any_content = True
                lines.append(f"\n### {label}\n```\n{content}\n```")
        except Exception:
            continue

    if not any_content:
        lines.append("(No documents committed to the init branch yet.)")

    return "\n".join(lines)


def inject_context(session_id: str = "", **kwargs: Any) -> dict | None:
    """Build a workspace/feature context block and return it for injection.

    The conversation loop appends the returned {"context": "..."} string to the
    current turn's user message.  workspace_id and feature_id are resolved from
    the per-session store the gateway router populates before each
    agent.run_conversation() call (keyed by the session_id the hook receives).
    """
    workspace_id, feature_id = get_context_for_session(session_id)

    if not workspace_id:
        logger.info(
            "workflow inject_context: no workspace context for session=%s — skipping injection",
            session_id,
        )
        return None

    parts = [
        "## Workflow context",
        f"workspace_id: {workspace_id}",
    ]
    if feature_id:
        parts.append(f"feature_id: {feature_id}")

    repo_ids: list[str] = []
    feature_stage: str = "unknown"

    if check_workflow_available():
        from .tools.workspace import handle as get_workspace
        from .tools.feature import handle as get_feature

        ws = get_workspace(workspace_id=workspace_id)
        if ws.get("ok"):
            repos = ws.get("workspace", {}).get("repos", [])
            repo_ids = [r.get("id", "") if isinstance(r, dict) else str(r) for r in repos]
            if repos:
                parts.append("repos: " + ", ".join(r for r in repo_ids if r))

        if feature_id:
            feat = get_feature(workspace_id=workspace_id, feature_id=feature_id)
            if feat.get("ok"):
                f = feat["feature"]
                if f.get("title"):
                    parts.append(f"feature_title: {f['title']}")
                if f.get("feature_name"):
                    parts.append(f"feature_name: {f['feature_name']}")
                feature_stage = f.get("stage", "unknown")
                parts.append(f"feature_stage: {feature_stage}")
                parts.append(f"feature_status: {f.get('status', 'unknown')}")
                if f.get("owner"):
                    parts.append(f"owner: {f['owner']}")
                if f.get("next_action"):
                    parts.append(f"next_action: {f['next_action']}")
                if f.get("init_pr_url") and f.get("feature_name"):
                    parts.append(f"init_pr_url: {f['init_pr_url']}")

                # Inject init PR document context so the agent knows the current
                # scaffold state before writing or approving any document.
                github_token = os.environ.get("GITHUB_TOKEN", "").strip()
                if github_token and f.get("init_pr_url") and f.get("feature_name"):
                    try:
                        from .tools.workspace import handle as _get_ws
                        from .tools.artifacts import _resolve_management_repo
                        ws_ctx = _get_ws(workspace_id=workspace_id)
                        if ws_ctx.get("ok"):
                            gh_owner, gh_repo = _resolve_management_repo(ws_ctx["workspace"])
                            init_block = _read_init_pr_context(
                                feature_name=f["feature_name"],
                                init_pr_url=f["init_pr_url"],
                                gh_owner=gh_owner,
                                gh_repo=gh_repo,
                                github_token=github_token,
                            )
                            if init_block:
                                parts.append(init_block)
                    except Exception as _exc:
                        logger.debug("inject_context: init PR read failed: %s", _exc)

            from .tools.tasks import handle as get_tasks

            result = get_tasks(workspace_id=workspace_id, feature_id=feature_id)
            if result.get("ok"):
                t_list = result["tasks"]
                task_lines: list[str] = []
                for t in t_list:
                    name = t.get("task_name", "")
                    title = t.get("title", "")
                    status = t.get("status", "")
                    line = f"  {name}: {title} [{status}]"
                    if t["status"] == "blocked" and t.get("blocked_reason"):
                        line += f" — blocked: {t['blocked_reason']}"
                    task_lines.append(line)
                if task_lines:
                    parts.append("tasks:\n" + "\n".join(task_lines))

    # Inject skill descriptions (G10)
    skills_block = _build_skills_block(feature_id, feature_stage, repo_ids)
    if skills_block:
        parts.append(skills_block)

    caps = ["get_tasks (live task status)"]
    if os.environ.get("GITNEXUS_MCP_URL"):
        caps.append("query_gitnexus (code structure / call graph / impact)")
    if os.environ.get("RAG_MCP_URL"):
        caps.append("query_rag (semantic recall over past specs/designs/logs)")
    if get_index():
        caps.append("load_skill (load full skill guidance on demand)")
    parts.append(
        "Before answering questions about task status, code structure, or prior decisions, "
        "use the workflow tools rather than guessing: " + "; ".join(caps) + ". "
        "Workspace and feature IDs are already set in context — omit them when calling tools."
    )
    parts.append(
        "IMPORTANT: Before calling write_product_spec, write_technical_design, or approve_feature, "
        "always review the Init PR context block above (if present) to understand the current "
        "scaffold state — existing spec content, current stage, and review status. "
        "Do not overwrite content that already exists unless explicitly asked."
    )

    return {"context": "\n".join(parts)}
