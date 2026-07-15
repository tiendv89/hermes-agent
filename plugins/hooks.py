"""pre_llm_call hook — returns workspace/feature context for injection into the user message."""

from __future__ import annotations

import logging
import os
from typing import Any

from .context import get_context_for_session
from src.services.workflow_backend_client import check_workflow_available
from .skills import get_index

logger = logging.getLogger(__name__)

# Repo-ID fragments → knowledge skill names (heuristic stack matching for G10).
# Matches on substrings of the repo id (case-insensitive).
_STACK_SKILL_MAP: list[tuple[str, list[str]]] = [
    (
        "ui",
        [
            "typescript-best-practices",
            "nextjs-best-practices",
            "heroui-react",
            "frontend-engineer",
        ],
    ),
    (
        "frontend",
        ["typescript-best-practices", "nextjs-best-practices", "frontend-engineer"],
    ),
    (
        "web",
        ["typescript-best-practices", "nextjs-best-practices", "frontend-engineer"],
    ),
    ("next", ["nextjs-best-practices", "typescript-best-practices"]),
    ("react", ["heroui-react", "typescript-best-practices", "frontend-engineer"]),
    ("mobile", ["react-native-mobile-engineer", "typescript-best-practices"]),
    ("backend", ["backend-engineer"]),
    ("api", ["backend-engineer"]),
    ("service", ["backend-engineer"]),
    ("hermes", ["python-best-practices", "backend-engineer"]),
    ("workflow", ["go-best-practices", "backend-engineer"]),
    ("go", ["go-best-practices"]),
    ("python", ["python-best-practices"]),
    ("data", ["python-data", "data-engineer"]),
    ("postgres", ["postgres-best-practices"]),
    ("nestjs", ["nestjs-best-practices", "typescript-best-practices"]),
    ("directus", ["directus-vue-engineer", "typescript-best-practices"]),
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


def _build_skills_block(
    feature_id: str, feature_stage: str, repo_ids: list[str]
) -> str | None:
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
            lines.append(
                "\n### Stack-matched skills for this feature's repos (load these first)"
            )
            for name, entry in matched:
                lines.append(f"  {name}: {entry.description}")

        remaining = [
            (n, e)
            for n, e in sorted(knowledge.items())
            if n not in {m[0] for m in matched}
        ]
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

        indexed_repos = None
        try:
            from .tools.gitnexus import list_indexed_repos

            indexed_repos = list_indexed_repos(timeout=10, workspace_id=workspace_id)
        except Exception as _exc:  # pragma: no cover - defensive
            logger.debug("inject_context: list_indexed_repos failed: %s", _exc)

        if indexed_repos:
            repo_ids = indexed_repos
            parts.append(
                "indexed repos (query/target these via query_gitnexus, passing repo=<name>): "
                + ", ".join(indexed_repos)
            )
        else:
            ws = get_workspace(workspace_id=workspace_id)
            if ws.get("ok"):
                repos = ws.get("workspace", {}).get("repos", [])
                repo_ids = [
                    r.get("id", "") if isinstance(r, dict) else str(r) for r in repos
                ]
                if repos:
                    parts.append("repos: " + ", ".join(r for r in repo_ids if r))
                    if indexed_repos is not None:
                        # GitNexus responded (indexed_repos == []) rather than
                        # being unreachable/misconfigured (indexed_repos is
                        # None) — so a workspace that does have repo(s)
                        # configured but zero indexed is most likely still
                        # indexing a freshly created/added repo, not a real
                        # absence. Say so explicitly rather than letting the
                        # silent fallback read as "everything's fine."
                        parts.append(
                            "note: GitNexus reports no indexed repos yet for this "
                            "workspace's configured repo(s) — if one was just "
                            "created, indexing may still be in progress; "
                            "query_gitnexus/query_rag may return nothing until it "
                            "completes. Don't conclude the repo doesn't exist."
                        )

        if not feature_id:
            # Non-feature session (Channel, Team Chat thread, DM).
            # workflow_lookup_feature is available for read-only feature lookups by ID/slug.
            parts.append(
                "session_kind: general (no feature scope) — "
                "use workflow_lookup_feature(feature_ref=<id-or-slug>) to look up "
                "a feature's title, stage, and synopsis without entering its scope."
            )

            try:
                from .tools.list_documents import handle as list_workspace_documents

                docs = list_workspace_documents(workspace_id=workspace_id)
                if docs.get("ok"):
                    files = docs.get("files", [])
                    folders = docs.get("folders", [])
                    if files or folders:
                        doc_lines = ["## Workspace files (uploaded at the workspace root)"]
                        if folders:
                            doc_lines.append("folders: " + ", ".join(folders))
                        if files:
                            doc_lines.append(
                                "files:\n"
                                + "\n".join(f"  {f['path']}" for f in files)
                            )
                        doc_lines.append(
                            "Use read_workspace_file(path=...) to read a file's content, or "
                            "list_documents(path=<folder>) to descend into a subfolder."
                        )
                        parts.append("\n".join(doc_lines))
                else:
                    logger.warning(
                        "inject_context: list_workspace_documents returned error: %s",
                        docs.get("error"),
                    )
                    parts.append(
                        "note: could not load the workspace file listing this turn "
                        "(list_documents error) — uploaded files may still exist; "
                        "call list_documents directly before concluding none do."
                    )
            except Exception as _exc:
                logger.warning("inject_context: list_workspace_documents failed: %s", _exc)
                parts.append(
                    "note: could not load the workspace file listing this turn "
                    "(unexpected error) — uploaded files may still exist; call "
                    "list_documents directly before concluding none do."
                )

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
