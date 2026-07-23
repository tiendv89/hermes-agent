"""Registers every tool this gateway exposes and builds the one API router.

Historically this was split into a "workflow" profile (BFF-proxied web
chat) and a "coding" profile (IDE pair-programming), each with its own
setup.py, running as two separate deployments distinguished by
``HERMES_PROFILE``. Both now run permanently in one process with one chat
surface (see src/app.py, src/api/routers/chat.py) — the split added no
value once there was nothing left to switch between, so it's gone. The
``_WORKFLOW_TOOLS``/``_CODING_TOOLS`` names are kept as separate tuples
(rather than flattened into one) only because they're registered under
different toolset defaults — see ``register_tools()`` below.
"""

from __future__ import annotations

import logging
from typing import Any

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
from plugins.tools import git_ops, local_file_ops, terminal
from src.services.workflow_backend_client import check_workflow_available

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool registry — workflow (document-editing, workflow-mutation, VCS) tools.
# ---------------------------------------------------------------------------

_WORKFLOW_TOOLS: tuple[dict[str, Any], ...] = (
    # These 6 tools are also registered below under _CODING_TOOLS with the
    # identical handler — pinned to "shared" (rather than this call's
    # "workflow" default) so both tool sets' agents can see them regardless
    # of registration order. See plugins/__init__.py::register()'s per-tool
    # toolset override.
    {
        "name": "get_workspace_context",
        "short_description": "See the workspace's repos, roles, environments, and docs.",
        "schema": workspace.SCHEMA,
        "handler": workspace.handle,
        "check_fn": check_workflow_available,
        "toolset": "shared",
    },
    {
        "name": "get_feature_state",
        "short_description": "Get a feature's title, stage, status, and next action.",
        "schema": feature.SCHEMA,
        "handler": feature.handle,
        "check_fn": check_workflow_available,
        "toolset": "shared",
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
        "toolset": "shared",
    },
    {
        "name": "query_gitnexus",
        "short_description": "Search the codebase for symbol definitions, callers, and impact.",
        "schema": gitnexus.SCHEMA,
        "handler": gitnexus.handle,
        "check_fn": gitnexus.check_available,
        "is_async": True,
        "toolset": "shared",
    },
    {
        "name": "query_rag",
        "short_description": "Semantic search over indexed product specs and technical designs.",
        "schema": rag.SCHEMA,
        "handler": rag.handle,
        "check_fn": rag.check_available,
        "is_async": True,
        "toolset": "shared",
    },
    {
        "name": "load_skill",
        "short_description": "Load a skill's full guidance and reference files.",
        "schema": skills_tool.SCHEMA,
        "handler": skills_tool.handle,
        "check_fn": skills_tool.check_available,
        "toolset": "shared",
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
# Tool registry — coding (IDE, client-executed/deferred) tools.
# ---------------------------------------------------------------------------

_CODING_TOOLS: tuple[dict[str, Any], ...] = (
    # ── Shared tools (server-executed) ────────────────────────────────
    # Also registered above under _WORKFLOW_TOOLS with the identical
    # handler — see that tuple's comment.
    {
        "name": "query_rag",
        "short_description": "Semantic search over indexed workspace documents.",
        "schema": rag.SCHEMA,
        "handler": rag.handle,
        "check_fn": rag.check_available,
        "is_async": True,
        "toolset": "shared",
    },
    {
        "name": "query_gitnexus",
        "short_description": "Search the codebase for symbol definitions, callers, and impact.",
        "schema": gitnexus.SCHEMA,
        "handler": gitnexus.handle,
        "check_fn": gitnexus.check_available,
        "is_async": True,
        "toolset": "shared",
    },
    {
        "name": "get_workspace_context",
        "short_description": "See the workspace's repos, roles, environments, and docs.",
        "schema": workspace.SCHEMA,
        "handler": workspace.handle,
        "toolset": "shared",
    },
    {
        "name": "get_feature_state",
        "short_description": "Get a feature's title, stage, status, and next action.",
        "schema": feature.SCHEMA,
        "handler": feature.handle,
        "toolset": "shared",
    },
    {
        "name": "get_tasks",
        "short_description": "See every task's status, blockers, and PR for the feature.",
        "schema": tasks_tool.SCHEMA,
        "handler": tasks_tool.handle,
        "toolset": "shared",
    },
    {
        "name": "load_skill",
        "short_description": "Load a skill's full guidance and reference files.",
        "schema": skills_tool.SCHEMA,
        "handler": skills_tool.handle,
        "check_fn": skills_tool.check_available,
        "toolset": "shared",
    },
    # ── Coding-only tools (client-executed, deferred) ─────────────────
    # File operations
    #
    # Registered under coding_-prefixed names — NOT "read_file"/"write_file"/
    # "edit_file" — because those names are also used by the doc-editing
    # tools above (plugins/tools/read.py, file_ops.py) with different
    # handlers. The vendored ToolRegistry is a single flat, name-keyed dict;
    # two different handlers can't both live under the same name. This only
    # renames what the model calls — the deferred-execution marker's own
    # "tool" field (see plugins/tools/deferred.py) still
    # hardcodes the original "read_file"/"write_file"/"edit_file" strings,
    # so the IDE extension's wire contract is unchanged.
    {
        "name": "coding_read_file",
        "short_description": "Read a file's content from the IDE workspace.",
        "schema": local_file_ops.READ_FILE_SCHEMA,
        "handler": local_file_ops.handle_read_file,
    },
    {
        "name": "coding_edit_file",
        "short_description": "Apply find-and-replace edits via the IDE's native editor API.",
        "schema": local_file_ops.EDIT_FILE_SCHEMA,
        "handler": local_file_ops.handle_edit_file,
    },
    {
        "name": "coding_write_file",
        "short_description": "Create or overwrite a file in the IDE workspace.",
        "schema": local_file_ops.WRITE_FILE_SCHEMA,
        "handler": local_file_ops.handle_write_file,
    },
    {
        "name": "create_directory",
        "short_description": "Create a directory (and parents) in the IDE workspace.",
        "schema": local_file_ops.CREATE_DIRECTORY_SCHEMA,
        "handler": local_file_ops.handle_create_directory,
    },
    {
        "name": "browse_directory",
        "short_description": "List files and subdirectories in a directory.",
        "schema": local_file_ops.BROWSE_DIRECTORY_SCHEMA,
        "handler": local_file_ops.handle_browse_directory,
    },
    {
        "name": "search_code",
        "short_description": "Search file contents for a regex pattern (grep).",
        "schema": local_file_ops.SEARCH_CODE_SCHEMA,
        "handler": local_file_ops.handle_search_code,
    },
    {
        "name": "search_files",
        "short_description": "Find files by glob pattern in the IDE workspace.",
        "schema": local_file_ops.SEARCH_FILES_SCHEMA,
        "handler": local_file_ops.handle_search_files,
    },
    # Terminal
    {
        "name": "run_command",
        "short_description": "Run a shell command in the IDE terminal.",
        "schema": terminal.RUN_COMMAND_SCHEMA,
        "handler": terminal.handle_run_command,
    },
    # Git operations
    {
        "name": "git_status",
        "short_description": "Get the working-tree status from the local git repo.",
        "schema": git_ops.GIT_STATUS_SCHEMA,
        "handler": git_ops.handle_git_status,
    },
    {
        "name": "git_diff",
        "short_description": "Get the unified diff of uncommitted changes.",
        "schema": git_ops.GIT_DIFF_SCHEMA,
        "handler": git_ops.handle_git_diff,
    },
    {
        "name": "git_commit",
        "short_description": "Commit staged changes with a message.",
        "schema": git_ops.GIT_COMMIT_SCHEMA,
        "handler": git_ops.handle_git_commit,
    },
    {
        "name": "git_push",
        "short_description": "Push commits to the remote.",
        "schema": git_ops.GIT_PUSH_SCHEMA,
        "handler": git_ops.handle_git_push,
    },
    {
        "name": "git_checkout",
        "short_description": "Switch to a branch (or create a new one).",
        "schema": git_ops.GIT_CHECKOUT_SCHEMA,
        "handler": git_ops.handle_git_checkout,
    },
    {
        "name": "git_log",
        "short_description": "Show recent commit history.",
        "schema": git_ops.GIT_LOG_SCHEMA,
        "handler": git_ops.handle_git_log,
    },
)


# ---------------------------------------------------------------------------
# Setup API — called once from src/app.py
# ---------------------------------------------------------------------------


def register_tools(ctx: Any) -> None:
    """Register every tool (workflow + coding) on the agent context.

    Two separate ``plugins.register()`` calls, not one flattened tuple:
    ``_WORKFLOW_TOOLS`` registers under the default "workflow" toolset,
    ``_CODING_TOOLS`` under "coding" — each tuple's own per-tool "shared"
    overrides still apply on top of that default. See
    ``plugins/__init__.py::register()``.
    """
    import plugins
    import tools.vision_tools  # noqa: F401  (self-registers on import)

    plugins.register(ctx, tools=_WORKFLOW_TOOLS)
    ctx.register_hook("pre_llm_call", inject_context)
    plugins.register(ctx, tools=_CODING_TOOLS, toolset="coding")
    logger.info(
        "setup: registered %d workflow tools + %d coding tools + vision_analyze + pre_llm_call hook",
        len(_WORKFLOW_TOOLS),
        len(_CODING_TOOLS),
    )


def build_router():
    """Return the one API router (chat, sessions, threads, channels, DMs, ...)."""
    from src.api.router import router

    return router
