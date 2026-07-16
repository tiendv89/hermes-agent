"""model_resolution — resolve tasks.md display-name model fields to model_id.

Called from both create_tasks.py and approve.py's step-d path immediately
before sending the task list to workflow-backend.  Substitutes each agent-actor
task's stored ``model`` display name (written into tasks.md at breakdown time)
with the matching ``model_id`` UUID from the current implementation-phase
candidate list.

Design ref: technical-design §6a (Option E — hermes-agent resolves, not backend).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)


def resolve_task_models(
    workspace_id: str,
    tasks: List[Dict[str, Any]],
    *,
    user_id: str = "",
    org_id: str = "",
) -> Dict[str, Any]:
    """Resolve display-name model fields in parsed task rows to ``model_id`` UUIDs.

    For every agent-actor task with a non-blank ``model`` field the function:
      1. Groups tasks by repo slug.
      2. For each unique repo, looks up its ``workspace_repos.id`` UUID, then
         calls ``GET .../model-policies/candidates?phase=implementation&repo=:uuid``
         to get the current candidate list (fresh re-fetch — catches any policy
         change since breakdown time).
      3. Case-sensitively matches the stored display name against
         ``candidates[].display_name``.  On a match, adds ``model_id`` to the
         task dict (the UUID sent to ``create_tasks``).
      4. On any unresolved name, returns a structured failure **without** calling
         ``create_tasks`` — the caller must surface the failure to the user.

    Human/either tasks (actor_type != "agent") and agent tasks with a blank
    ``model`` field are passed through unchanged; they contribute no ``model_id``
    to the payload.

    If the candidates endpoint is unavailable (``WorkflowBackendError`` or any
    exception), the function **skips** resolution for that repo and returns the
    tasks without ``model_id`` entries, so the pipeline degrades gracefully when
    the endpoint has not yet been deployed (T5 of this feature).

    Args:
        workspace_id: workspace UUID or slug.
        tasks: parsed task rows from ``parse_tasks_index``.  Each row has at
               minimum: ``name``, ``actor_type``, ``repo``, ``model`` (may be "").
        user_id: caller user id forwarded to API headers.
        org_id: caller org id forwarded to API headers.

    Returns:
        ``{"ok": True, "tasks": [...]}`` — every task in the input list; agent
        tasks whose display name resolved have an added ``model_id`` field.

        ``{"ok": False, "unresolved": [...]}`` — one or more display names did
        not match any current candidate.  Each entry:
          ``{"task_name": str, "display_name": str, "valid_alternatives": [str]}``
        No ``model_id`` substitution is made for any task in this case (the
        caller should abort and report the failure).
    """
    from src.services.workflow_backend_client import (
        WorkflowBackendError,
        get_implementation_candidates,
        get_workspace_repo_by_slug,
        run_async,
    )

    # Collect agent tasks that carry a non-blank model display name.
    agent_tasks_with_model = [
        t for t in tasks
        if t.get("actor_type") == "agent" and (t.get("model") or "").strip()
    ]

    if not agent_tasks_with_model:
        # Nothing to resolve — return the input list unchanged.
        return {"ok": True, "tasks": list(tasks)}

    # Step 1: unique repo slugs among those tasks.
    repos = {t["repo"] for t in agent_tasks_with_model if t.get("repo")}

    # Step 2: for each repo fetch candidates (one call per unique repo).
    candidates_by_repo: Dict[str, List[Dict[str, Any]]] = {}
    for repo_slug in repos:
        try:
            repo_uuid = run_async(
                get_workspace_repo_by_slug(
                    workspace_id, repo_slug, user_id=user_id or None, org_id=org_id or None
                )
            )
            if not repo_uuid:
                logger.warning(
                    "resolve_task_models: repo %r not found in workspace %r — skipping",
                    repo_slug,
                    workspace_id,
                )
                candidates_by_repo[repo_slug] = []
                continue
            result = run_async(
                get_implementation_candidates(
                    workspace_id, repo_uuid, user_id=user_id or None, org_id=org_id or None
                )
            )
            candidates_by_repo[repo_slug] = result.get("candidates") or []
        except WorkflowBackendError as exc:
            logger.warning(
                "resolve_task_models: candidates fetch failed for repo %r: %s — skipping",
                repo_slug,
                exc,
            )
            candidates_by_repo[repo_slug] = []
        except Exception as exc:
            logger.warning(
                "resolve_task_models: unexpected error for repo %r: %s — skipping",
                repo_slug,
                exc,
            )
            candidates_by_repo[repo_slug] = []

    # Step 3: match each agent task's display name against the candidate list.
    unresolved: List[Dict[str, Any]] = []
    resolved_tasks: List[Dict[str, Any]] = []

    for task in tasks:
        task_copy = dict(task)
        if task.get("actor_type") == "agent" and (task.get("model") or "").strip():
            repo_slug = task.get("repo", "")
            display_name = task["model"].strip()
            candidates = candidates_by_repo.get(repo_slug, [])

            if not candidates:
                # Endpoint unavailable / repo not found — skip gracefully.
                resolved_tasks.append(task_copy)
                continue

            # Case-sensitive exact match (models.display_name semantics).
            match = next(
                (c for c in candidates if c.get("display_name") == display_name),
                None,
            )
            if match:
                task_copy["model_id"] = match["model_id"]
            else:
                valid_alternatives = [
                    c.get("display_name", "") for c in candidates if c.get("display_name")
                ]
                unresolved.append(
                    {
                        "task_name": task["name"],
                        "display_name": display_name,
                        "valid_alternatives": valid_alternatives,
                    }
                )

        resolved_tasks.append(task_copy)

    if unresolved:
        return {"ok": False, "unresolved": unresolved}

    return {"ok": True, "tasks": resolved_tasks}


def format_unresolved_error(unresolved: List[Dict[str, Any]]) -> str:
    """Format an unresolved-models failure list into a human-readable message."""
    lines = []
    for u in unresolved:
        alts = u.get("valid_alternatives") or []
        alt_str = ", ".join(f'"{a}"' for a in alts) if alts else "none available"
        lines.append(
            f"  - Task {u['task_name']}: model {u['display_name']!r} is not currently "
            f"in the workspace's implementation policy. "
            f"Valid alternatives: {alt_str}."
        )
    return (
        "One or more tasks reference a model not currently in the workspace's "
        "implementation-phase policy. Update the model selection in tasks.md and "
        "re-run approve.\n" + "\n".join(lines)
    )
