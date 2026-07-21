"""
Feature context.
Called by inject_context at session start (when feature_id is present)
and exposed as the `get-feature-context` skill for on-demand refresh.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from src.services.workflow_backend_client import (
    check_workflow_available,
    get_feature_detail,
    get_feature_tasks,
    run_async,
)

logger = logging.getLogger(__name__)


class _Missing:
    """Sentinel for a failed/missing fetch — not an exception."""

    def __init__(self, reason: str) -> None:
        self.reason = reason


def get_feature_context() -> str:
    """Fetch full feature state: lifecycle, tasks, product spec, technical design.

    Runs all four fetches in parallel via a thread pool. Returns a Markdown
    block for injection into the system prompt.  Failures are non-blocking —
    missing pieces are noted rather than preventing other data from loading.
    """
    from plugins.context import get_feature_id, get_org_id, get_user_id, get_workspace_id

    workspace_id = get_workspace_id()
    feature_id = get_feature_id()
    if not feature_id:
        return ""

    user_id = get_user_id()
    org_id = get_org_id()

    # Run all four fetches in parallel via a thread pool.
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        future_map = {
            executor.submit(
                _fetch_feature_state, org_id, user_id, workspace_id, feature_id
            ): "state",
            executor.submit(
                _fetch_tasks, org_id, user_id, workspace_id, feature_id
            ): "tasks",
            executor.submit(
                _fetch_document,
                workspace_id,
                feature_id,
                "product_spec.md",
                user_id,
                org_id,
            ): "product_spec",
            executor.submit(
                _fetch_document,
                workspace_id,
                feature_id,
                "technical_design.md",
                user_id,
                org_id,
            ): "technical_design",
        }
        for future in as_completed(future_map):
            key = future_map[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                results[key] = _Missing(f"unexpected error: {exc}")

    return _format_context_block(
        feature_id=feature_id,
        state=results.get("state"),
        tasks=results.get("tasks"),
        product_spec=results.get("product_spec"),
        technical_design=results.get("technical_design"),
    )


def _fetch_feature_state(
    org_id: str, user_id: str, workspace_id: str, feature_id: str
) -> Any:
    """Fetch lifecycle stage/status from workflow-backend."""
    if not check_workflow_available():
        return _Missing("workflow-backend unavailable")
    try:
        return run_async(
            get_feature_detail(
                workspace_id, feature_id, user_id=user_id, org_id=org_id
            )
        )
    except Exception as exc:
        return _Missing(f"get_feature_state failed: {exc}")


def _fetch_tasks(
    org_id: str, user_id: str, workspace_id: str, feature_id: str
) -> Any:
    """Fetch live task statuses from workflow-backend."""
    if not check_workflow_available():
        return _Missing("workflow-backend unavailable")
    try:
        return run_async(
            get_feature_tasks(
                workspace_id, feature_id, user_id=user_id, org_id=org_id
            )
        )
    except Exception as exc:
        return _Missing(f"get_tasks failed: {exc}")


def _fetch_document(
    workspace_id: str,
    feature_id: str,
    doc_path: str,
    user_id: str,
    org_id: str,
) -> Any:
    """Fetch a canonical document from storage-service."""
    try:
        from plugins.clients.storage_service_client import read_document_content

        result = read_document_content(
            workspace_id=workspace_id,
            feature_id=feature_id,
            path=doc_path,
            user_id=user_id,
            org_id=org_id,
        )
        content = result.get("content", "")
        if not content:
            return _Missing(f"no {doc_path.replace('.md', '')} yet")
        return content
    except Exception as exc:
        return _Missing(f"read {doc_path} failed: {exc}")


def _format_context_block(
    *,
    feature_id: str,
    state: Any,
    tasks: Any,
    product_spec: Any,
    technical_design: Any,
) -> str:
    """Format all fetched state into a Markdown block."""
    lines = ["## Feature Context (auto-loaded)", ""]

    # --- Lifecycle state ---
    lines.append("### Lifecycle")
    if isinstance(state, _Missing):
        lines.append(f"(unavailable: {state.reason})")
    elif isinstance(state, dict):
        lines.append(f"- Stage: {state.get('stage', 'unknown')}")
        lines.append(f"- Status: {state.get('status', 'unknown')}")
        owner = state.get("owner")
        if owner:
            lines.append(f"- Owner: {owner}")
    lines.append("")

    # --- Live tasks ---
    lines.append("### Tasks (live status)")
    if isinstance(tasks, _Missing):
        lines.append(f"(unavailable: {tasks.reason})")
    elif isinstance(tasks, list):
        if not tasks:
            lines.append("No tasks created yet.")
        else:
            lines.append(
                "| Task | Title | Status | Depends On | PR |"
            )
            lines.append("|---|---|---|---|---|")
            for t in tasks:
                pr_url = "—"
                pr = t.get("pr")
                if pr and isinstance(pr, dict):
                    pr_url = pr.get("url", "—")
                task_name = t.get("task_name", "?")
                title = t.get("title", "?")
                status = t.get("status", "?")
                depends_on = t.get("depends_on") or []
                if isinstance(depends_on, list):
                    deps_str = ", ".join(depends_on) if depends_on else "—"
                else:
                    deps_str = str(depends_on)
                lines.append(
                    f"| {task_name} | {title} | {status} | {deps_str} | {pr_url} |"
                )
                blocked_reason = t.get("blocked_reason")
                if blocked_reason:
                    lines.append(f"  ⛔ **Blocked:** {blocked_reason}")
                    blocked_suggestion = t.get("blocked_suggestion")
                    if blocked_suggestion:
                        lines.append(f"     → {blocked_suggestion}")
    lines.append("")

    # --- Product spec (summary) ---
    lines.append("### Product Spec")
    if isinstance(product_spec, _Missing):
        lines.append(f"(unavailable: {product_spec.reason})")
    elif product_spec:
        lines.append(_summarize_markdown(str(product_spec), max_lines=20))
    lines.append("")

    # --- Technical design (summary) ---
    lines.append("### Technical Design")
    if isinstance(technical_design, _Missing):
        lines.append(f"(unavailable: {technical_design.reason})")
    elif technical_design:
        lines.append(_summarize_markdown(str(technical_design), max_lines=20))
    lines.append("")

    # --- Footer ---
    lines.append(
        "Use `get_feature_state`, `get_tasks`, and `read_file` for full details."
    )
    lines.append("")

    return "\n".join(lines)


def _summarize_markdown(content: str, max_lines: int = 20) -> str:
    """Return a truncated version of a markdown document for injection.

    Keeps headings and enough body to be useful without blowing the prompt.
    """
    all_lines = content.split("\n")
    if len(all_lines) <= max_lines:
        return content
    truncated = "\n".join(all_lines[:max_lines])
    remaining = len(all_lines) - max_lines
    return f"{truncated}\n\n*(+{remaining} more lines — use read_file for full content)*"
