"""Workflow profile — registers all workflow tools and mounts the existing router.

The workflow profile serves the BFF-proxied web chat with the full set of
document-editing, workflow-mutation, and VCS tools. It uses BFF-injected
headers (X-User-Id, X-Org-Id) for authentication.
"""

from __future__ import annotations

import logging
from typing import Any

from src.services.workflow_backend_client import check_workflow_available
from plugins.hooks import inject_context
from plugins.tools import (
    approval,
    approve as approve_tool,
    artifacts,
    commit_files as commit_files_tool,
    create_pr as create_pr_tool,
    create_tasks as create_tasks_tool,
    edit as edit_tool,
    ensure_branch as ensure_branch_tool,
    feature,
    file_ops as file_ops_tool,
    gitnexus,
    init_feature as init_feature_tool,
    list_documents as list_documents_tool,
    lookup_feature as lookup_feature_tool,
    move_feature as move_feature_tool,
    parse_tasks as parse_tasks_tool,
    rag,
    read as read_tool,
    read_workspace_file as read_workspace_file_tool,
    skills as skills_tool,
    suggest_next_actions as suggest_next_actions_tool,
    tasks as tasks_tool,
    tasks_write as tasks_write_tool,
    vcs_pr_context as vcs_pr_context_tool,
    vcs_pr_review as vcs_pr_review_tool,
    workspace,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool registry — the complete set of workflow tools.
# ---------------------------------------------------------------------------

_WORKFLOW_TOOLS: tuple[dict[str, Any], ...] = (
    {
        "name": "get_workspace_context",
        "short_description": "See the workspace's repos, roles, environments, and docs.",
        "schema": workspace.SCHEMA,
        "handler": workspace.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "get_feature_state",
        "short_description": "Get a feature's title, stage, status, and next action.",
        "schema": feature.SCHEMA,
        "handler": feature.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "write_file",
        "short_description": "Create or overwrite a file in the workspace or a feature's folder.",
        "schema": file_ops_tool.WRITE_FILE_SCHEMA,
        "handler": file_ops_tool.handle_write_file,
        "check_fn": check_workflow_available,
    },
    {
        "name": "edit_file",
        "short_description": "Make find-and-replace edits to an existing file.",
        "schema": file_ops_tool.EDIT_FILE_SCHEMA,
        "handler": file_ops_tool.handle_edit_file,
        "check_fn": check_workflow_available,
    },
    {
        "name": "write_product_spec",
        "short_description": "Write or revise the feature's product spec.",
        "schema": artifacts.WRITE_SPEC_SCHEMA,
        "handler": artifacts.handle_write_product_spec,
        "check_fn": check_workflow_available,
    },
    {
        "name": "read_file",
        "short_description": "Read a feature's product spec, technical design, or status.",
        "schema": read_tool.READ_FILE_SCHEMA,
        "handler": read_tool.handle_read_file,
        "check_fn": check_workflow_available,
    },
    {
        "name": "edit_document",
        "short_description": "Make find-and-replace edits to the product spec or technical design.",
        "schema": edit_tool.EDIT_DOCUMENT_SCHEMA,
        "handler": edit_tool.handle_edit_document,
        "check_fn": check_workflow_available,
    },
    {
        "name": "read_workspace_file",
        "short_description": "Read a file uploaded to the workspace's Files browser.",
        "schema": read_workspace_file_tool.SCHEMA,
        "handler": read_workspace_file_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "list_documents",
        "short_description": "Browse a workspace's document folders and files.",
        "schema": list_documents_tool.SCHEMA,
        "handler": list_documents_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "write_technical_design",
        "short_description": "Write or revise the feature's technical design.",
        "schema": artifacts.WRITE_TD_SCHEMA,
        "handler": artifacts.handle_write_technical_design,
        "check_fn": check_workflow_available,
    },
    {
        "name": "get_tasks",
        "short_description": "See every task's status, blockers, and PR for the feature.",
        "schema": tasks_tool.SCHEMA,
        "handler": tasks_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "query_gitnexus",
        "short_description": "Search the codebase for symbol definitions, callers, and impact.",
        "schema": gitnexus.SCHEMA,
        "handler": gitnexus.handle,
        "check_fn": gitnexus.check_available,
        "is_async": True,
    },
    {
        "name": "query_rag",
        "short_description": "Semantic search over indexed product specs and technical designs.",
        "schema": rag.SCHEMA,
        "handler": rag.handle,
        "check_fn": rag.check_available,
        "is_async": True,
    },
    {
        "name": "load_skill",
        "short_description": "Load a skill's full guidance and reference files.",
        "schema": skills_tool.SCHEMA,
        "handler": skills_tool.handle,
        "check_fn": skills_tool.check_available,
    },
    {
        "name": "request_approval",
        "short_description": "Ask a human to approve, reject, or reopen a lifecycle stage.",
        "schema": approval.SCHEMA,
        "handler": approval.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "approve_feature",
        "short_description": "Approve, reject, or reopen a feature lifecycle stage.",
        "schema": approve_tool.SCHEMA,
        "handler": approve_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "move_feature_status",
        "schema": move_feature_tool.SCHEMA,
        "handler": move_feature_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "write_tasks",
        "short_description": "Generate and write the feature's task breakdown.",
        "schema": tasks_write_tool.SCHEMA,
        "handler": tasks_write_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "create_tasks",
        "short_description": "Backup: create DB tasks after a partial approve failure.",
        "schema": create_tasks_tool.SCHEMA,
        "handler": create_tasks_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "parse_tasks",
        "short_description": "Parse tasks.md into a structured task list (read-only).",
        "schema": parse_tasks_tool.SCHEMA,
        "handler": parse_tasks_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "suggest_next_actions",
        "short_description": "Suggest 1-3 next actions the user could take.",
        "schema": suggest_next_actions_tool.SCHEMA,
        "handler": suggest_next_actions_tool.handle,
    },
    {
        "name": "vcs_pr_context",
        "short_description": "Read a GitHub PR's diff, files, comments, reviews, and CI status.",
        "schema": vcs_pr_context_tool.SCHEMA,
        "handler": vcs_pr_context_tool.handle,
        "check_fn": vcs_pr_context_tool.check_available,
    },
    {
        "name": "vcs_pr_review",
        "short_description": "Post an APPROVE or REQUEST_CHANGES review on a GitHub PR.",
        "schema": vcs_pr_review_tool.SCHEMA,
        "handler": vcs_pr_review_tool.handle,
        "check_fn": vcs_pr_review_tool.check_available,
    },
    {
        "name": "workflow_lookup_feature",
        "short_description": "Look up a feature's title, stage, status, and synopsis.",
        "schema": lookup_feature_tool.SCHEMA,
        "handler": lookup_feature_tool.handle,
        "check_fn": lookup_feature_tool.check_available,
    },
    {
        "name": "workflow_init_feature",
        "short_description": "Create a new feature in the current workspace.",
        "schema": init_feature_tool.SCHEMA,
        "handler": init_feature_tool.handle,
        "check_fn": check_workflow_available,
    },
    {
        "name": "vcs_create_pr",
        "short_description": "Open a pull request from one branch into another.",
        "schema": create_pr_tool.SCHEMA,
        "handler": create_pr_tool.handle,
        "check_fn": create_pr_tool.check_available,
    },
    {
        "name": "vcs_ensure_branch",
        "short_description": "Create a branch from a base branch if it doesn't exist yet.",
        "schema": ensure_branch_tool.SCHEMA,
        "handler": ensure_branch_tool.handle,
        "check_fn": ensure_branch_tool.check_available,
    },
    {
        "name": "vcs_commit_files",
        "short_description": "Commit one or more files directly to a branch.",
        "schema": commit_files_tool.SCHEMA,
        "handler": commit_files_tool.handle,
        "check_fn": commit_files_tool.check_available,
    },
)


# ---------------------------------------------------------------------------
# Profile API
# ---------------------------------------------------------------------------


def register_tools(ctx: Any) -> None:
    """Register all workflow tools on the agent context.

    Delegates tool registration to the shared ``plugins.register()`` entry
    point, then registers the ``pre_llm_call`` hook for workspace/feature
    context injection.
    """
    import plugins

    plugins.register(ctx, tools=_WORKFLOW_TOOLS)
    ctx.register_hook("pre_llm_call", inject_context)
    logger.info(
        "workflow profile: registered %d tools + pre_llm_call hook",
        len(_WORKFLOW_TOOLS),
    )


def build_router():
    """Return the existing workflow API router.

    The router aggregates all sub-routers (chat, sessions, threads, channels,
    DMs, members, tools, etc.) that serve the existing BFF-proxied endpoints.
    """
    from src.api.router import router

    return router
