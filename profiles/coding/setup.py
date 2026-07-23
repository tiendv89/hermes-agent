"""Coding profile — registers client-executed tools with deferred execution.

The coding profile serves the IDE pair-programming use case.  Its tools
return deferred-execution markers (``{"__deferred__": True, ...}``) that
the IDE extension picks up via SSE, executes locally, and reports back.

Shared tools (RAG, GitNexus, workflow-backend queries, load_skill) are
imported from ``plugins/tools/``.  Coding-specific tools live in
``profiles/coding/tools/``.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from plugins.tools import (
    feature,
    gitnexus,
    rag,
    skills as skills_tool,
    tasks as tasks_tool,
    workspace,
)
from profiles.coding.tools import git_ops, local_file_ops, terminal
from src.api.deps import get_db
from src.api.model_catalog import get_active_models
from src.db.store import get_default_catalog_model

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

_CODING_TOOLS: tuple[dict[str, Any], ...] = (
    # ── Shared tools (server-executed) ────────────────────────────────
    {
        "name": "query_rag",
        "short_description": "Semantic search over indexed workspace documents.",
        "schema": rag.SCHEMA,
        "handler": rag.handle,
        "check_fn": rag.check_available,
        "is_async": True,
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
        "name": "get_workspace_context",
        "short_description": "See the workspace's repos, roles, environments, and docs.",
        "schema": workspace.SCHEMA,
        "handler": workspace.handle,
    },
    {
        "name": "get_feature_state",
        "short_description": "Get a feature's title, stage, status, and next action.",
        "schema": feature.SCHEMA,
        "handler": feature.handle,
    },
    {
        "name": "get_tasks",
        "short_description": "See every task's status, blockers, and PR for the feature.",
        "schema": tasks_tool.SCHEMA,
        "handler": tasks_tool.handle,
    },
    {
        "name": "load_skill",
        "short_description": "Load a skill's full guidance and reference files.",
        "schema": skills_tool.SCHEMA,
        "handler": skills_tool.handle,
        "check_fn": skills_tool.check_available,
    },
    # ── Coding-only tools (client-executed, deferred) ─────────────────
    # File operations
    {
        "name": "read_file",
        "short_description": "Read a file's content from the IDE workspace.",
        "schema": local_file_ops.READ_FILE_SCHEMA,
        "handler": local_file_ops.handle_read_file,
    },
    {
        "name": "edit_file",
        "short_description": "Apply find-and-replace edits via the IDE's native editor API.",
        "schema": local_file_ops.EDIT_FILE_SCHEMA,
        "handler": local_file_ops.handle_edit_file,
    },
    {
        "name": "write_file",
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
# Profile API
# ---------------------------------------------------------------------------


def register_tools(ctx: Any) -> None:
    """Register coding-profile tools on the agent context.

    Shared tools (RAG, GitNexus, workflow-backend) are server-executed
    and wrapped with the standard JSON-result + guardrail pipeline.
    Coding tools are client-executed (deferred) — their handlers return a
    ``__deferred__`` marker that the ``_json_result_handler`` detects and
    passes through unmodified.
    """
    import plugins

    plugins.register(ctx, tools=_CODING_TOOLS)
    logger.info(
        "coding profile: registered %d tools (%d shared, %d coding)",
        len(_CODING_TOOLS),
        6,
        len(_CODING_TOOLS) - 6,
    )


def build_router():
    """Return an APIRouter for the coding profile.

    Mounts:

    * ``POST /coding/chat`` — SSE endpoint (JWT auth, deferred tools).
    * ``GET  /coding/version`` — public, per-IDE version info.
    * ``GET  /coding/models`` — selectable chat models for the IDE's model
      picker, sourced from the same ``model_catalog`` table the browser
      app's picker uses (see src/api/model_catalog.py).
    """
    import os

    from fastapi import APIRouter

    from src.api.routers.coding_chat import router as chat_router

    router = APIRouter()

    # ── Coding chat (SSE, JWT auth, deferred tools) ──────────────────
    router.include_router(chat_router)

    # ── Model catalog (coding_jwt_auth — same gate as /coding/chat) ──
    @router.get("/coding/models")
    async def coding_models(db: Annotated[AsyncSession, Depends(get_db)]):
        models = await get_active_models(db)
        default_row = await get_default_catalog_model(db)
        return {
            "models": models,
            "default": default_row.model_id if default_row else "",
        }

    # ── Version endpoint (public, no auth) ───────────────────────────
    @router.get("/coding/version")
    async def coding_version():
        return {
            "vscode": {
                "min_version": os.environ.get(
                    "CODING_VSCODE_MIN_VERSION", "1.0.0"
                ),
                "recommended_version": os.environ.get(
                    "CODING_VSCODE_RECOMMENDED_VERSION", "1.0.0"
                ),
                "marketplace_url": os.environ.get(
                    "CODING_VSCODE_MARKETPLACE_URL",
                    "https://marketplace.visualstudio.com/items?itemName=nousresearch.hermes",
                ),
                "deprecation_notice": os.environ.get(
                    "CODING_VSCODE_DEPRECATION_NOTICE"
                )
                or None,
            },
            "jetbrains": {
                "min_version": os.environ.get(
                    "CODING_JETBRAINS_MIN_VERSION", "1.0.0"
                ),
                "recommended_version": os.environ.get(
                    "CODING_JETBRAINS_RECOMMENDED_VERSION", "1.0.0"
                ),
                "marketplace_url": os.environ.get(
                    "CODING_JETBRAINS_MARKETPLACE_URL",
                    "https://plugins.jetbrains.com/plugin/nousresearch-hermes",
                ),
                "deprecation_notice": os.environ.get(
                    "CODING_JETBRAINS_DEPRECATION_NOTICE"
                )
                or None,
            },
        }

    return router
