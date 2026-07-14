"""write_tasks tool — generate task breakdown for a feature.

Writes tasks.md (narrative only) to storage-service — no git involved.
Tasks are created in the DB at tasks-stage approval (not during breakdown).
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from ..skills import get_index
from ..storage_service_client import StorageServiceError, write_document_content
from ..validation import _validate_id

logger = logging.getLogger(__name__)

_TASK_ID_RE = re.compile(r"^T\d+$")

# Matches a "### Required skills" subsection body, up to the next ## or ###
# heading (or end of string). Section content, not the tasks.md structure
# itself, is the source of truth for per-task skills (see shared.md).
_REQUIRED_SKILLS_SECTION_RE = re.compile(
    r"^###[ \t]*Required skills[ \t]*$\n(.*?)(?=^#{2,3}[ \t]|\Z)",
    re.MULTILINE | re.DOTALL,
)
_SKILL_BULLET_RE = re.compile(
    r"^[ \t]*-[ \t]*`?([a-z0-9][a-z0-9-]*)`?[ \t]*$", re.MULTILINE
)


def _extract_required_skills(tasks_md: str) -> set[str]:
    """Collect every skill slug referenced across all '### Required skills' subsections."""
    skills: set[str] = set()
    for section in _REQUIRED_SKILLS_SECTION_RE.findall(tasks_md):
        skills.update(_SKILL_BULLET_RE.findall(section))
    return skills


SCHEMA: Dict[str, Any] = {
    "description": (
        "Generate the task breakdown for a feature and write it to storage-service. "
        "Writes tasks.md only. Tasks are created in the DB at "
        "tasks-stage approval (via approve_feature or the backup /create-tasks command) — "
        "not during breakdown. "
        "REQUIRED FIRST: call read_file(document='technical_design') (and 'product_spec') to "
        "load the approved design from the feature branch and derive the task list from its actual "
        "content — never infer tasks from RAG or the request text. "
        "Each task's 'repo' MUST be a real repo name from query_gitnexus(tool='list_repos'); "
        "determine it by querying GitNexus for the symbols/files the task touches and using the "
        "repo that contains them — do NOT guess the repo from the feature title or use workspace.yaml. "
        "If the technical design leaves the breakdown itself ambiguous — unclear task ownership "
        "(agent vs. human), unclear sequencing/dependencies, or a scope split GitNexus can't "
        "resolve — use the clarify tool to ask the user before writing, rather than guessing "
        "(interactive sessions only — skip clarify when AGENT_RUNTIME=1 and note the ambiguity "
        "in the task instead). "
        "Call this after technical_design is approved and you have designed the full task list."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "tasks": {
                "type": "array",
                "description": (
                    "Ordered list of tasks. Each task must have an id (T1, T2, ...), "
                    "title, and optionally: repo, depends_on (list of task IDs), "
                    "actor_type ('agent' | 'human' | 'either')."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "Task ID, e.g. T1"},
                        "title": {"type": "string"},
                        "repo": {
                            "type": "string",
                            "description": "Repo slug this task targets.",
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Task IDs this task depends on. Empty means it can start immediately.",
                        },
                        "actor_type": {
                            "type": "string",
                            "enum": ["agent", "human", "either"],
                            "description": "Who executes this task. Defaults to 'agent'.",
                        },
                    },
                    "required": ["id", "title"],
                    "additionalProperties": False,
                },
                "minItems": 1,
            },
            "tasks_md": {
                "type": "string",
                "description": (
                    "Full narrative tasks.md content — dependency diagram, index table, "
                    "and per-task sections (## T{n} — {Title} with Description, "
                    "Required skills, Subtasks). This is the human-readable breakdown."
                ),
            },
            "commit_message": {
                "type": "string",
                "description": "Git commit message (optional).",
            },
            "workspace_id": {
                "type": "string",
                "description": "Workspace identifier. Omit to use the current workspace from context.",
            },
            "feature_id": {
                "type": "string",
                "description": "Feature identifier. Omit to use the current feature from context.",
            },
        },
        "required": ["tasks", "tasks_md"],
        "additionalProperties": False,
    },
}


def handle(
    tasks: List[Dict[str, Any]],
    tasks_md: str,
    commit_message: str = "",
    workspace_id: str = "",
    feature_id: str = "",
    **_: Any,
) -> Dict[str, Any]:
    from ..context import get_feature_id, get_org_id, get_user_id, get_workspace_id

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

    if not tasks:
        return {"ok": False, "error": "tasks must be a non-empty list."}

    if not tasks_md or not tasks_md.strip():
        return {"ok": False, "error": "tasks_md (narrative markdown) is required."}

    # Validate task IDs
    for t in tasks:
        tid = t.get("id", "")
        if not _TASK_ID_RE.match(tid):
            return {
                "ok": False,
                "error": f"Invalid task id {tid!r}. Must match T<number>, e.g. T1, T2.",
            }

    # Validate each task's repo against GitNexus's indexed repo set — the
    # authoritative repo universe. This is the guardrail behind the "determine
    # repo from GitNexus, not guesswork" rule: reject tasks pointed at a repo
    # that isn't actually indexed. Skipped gracefully when GitNexus is
    # unavailable (list_indexed_repos returns None) so authoring still works.
    from .gitnexus import list_indexed_repos

    # GitNexus only serves workspace-scoped endpoints (/ws/<slug>/sse), so the
    # session workspace must be passed — without it the lookup is skipped and
    # validation degrades gracefully.
    indexed_repos = list_indexed_repos(workspace_id=get_workspace_id())
    repo_validation_note = ""
    if indexed_repos:
        indexed_set = set(indexed_repos)
        unknown = sorted(
            {
                (t.get("repo") or "").strip()
                for t in tasks
                if (t.get("repo") or "").strip()
            }
            - indexed_set
        )
        if unknown:
            return {
                "ok": False,
                "error": (
                    f"Task repo(s) not indexed in GitNexus: {unknown}. "
                    f"Valid repos: {sorted(indexed_set)}. "
                    "Set each task's repo to the GitNexus repo that actually contains the code it "
                    "touches — call query_gitnexus(tool='list_repos') and query the relevant symbols "
                    "to confirm. Do not guess the repo from the feature title."
                ),
            }
    elif indexed_repos is not None:
        # GitNexus responded but reported zero indexed repos (as opposed to
        # None, meaning unreachable/misconfigured) — most likely a freshly
        # created repo still indexing. Skip validation as with "unavailable",
        # but flag it in the response so the skip isn't read as a clean pass.
        repo_validation_note = (
            " (repo validation skipped: GitNexus reports no indexed repos yet "
            "for this workspace — if a repo was just created, indexing may "
            "still be in progress)"
        )

    # Validate every "### Required skills" slug against the live technical_skills
    # index — the same guardrail as the repo check above, but for skills. Without
    # this, an invented slug (e.g. "react-best-practices") only surfaces much later
    # as a task_skipped_missing_skill failure at run-task time.
    knowledge_skills = {name for name, e in get_index().items() if not e.is_authoring}
    referenced_skills = _extract_required_skills(tasks_md)
    unknown_skills = sorted(referenced_skills - knowledge_skills)
    if unknown_skills:
        return {
            "ok": False,
            "error": (
                f"Unknown skill slug(s) in Required skills: {unknown_skills}. "
                f"Valid slugs: {sorted(knowledge_skills)}. "
                "Only use slugs from the '## Available skills' block injected in "
                "context — do not invent or guess names."
            ),
        }

    try:
        _validate_id(fid, "feature_id")
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        result = write_document_content(
            wid,
            fid,
            "tasks.md",
            tasks_md,
            user_id=caller_user_id,
            org_id=caller_org_id,
        )
    except StorageServiceError as exc:
        logger.warning("write_tasks: storage-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("write_tasks: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "owner": "go",
        "branch": None,
        "commit_sha": None,
        "version_id": result.get("version_id"),
        "tasks_committed": len(tasks),
        "files_written": [f"storage-service://{wid}/{fid}/tasks.md"],
        "message": (
            f"Task breakdown written: {len(tasks)} tasks written to storage-service. "
            "Tasks will be created in the DB at tasks-stage approval "
            "(via approve_feature(stage='tasks') or the backup /create-tasks command). "
            "Call approve_feature(stage='tasks') when ready to activate tasks."
            + repo_validation_note
        ),
    }
