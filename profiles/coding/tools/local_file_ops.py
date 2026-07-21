"""Coding-profile file-operation tools — deferred execution.

Every handler returns a ``{"__deferred__": True, "tool": "...", "params": {...}}``
marker.  The IDE extension executes the tool locally and returns the real result.

Tools
-----
* ``read_file``        — read a file's content (optional line range)
* ``edit_file``        — find-and-replace edits via native editor API
* ``write_file``       — create or overwrite a file
* ``create_directory`` — create a directory (including parents)
* ``browse_directory`` — list directory entries
* ``search_code``      — grep / ripgrep content search
* ``search_files``     — glob-based filename search
"""

from __future__ import annotations

from typing import Any

from profiles.coding.tools import deferred

# ---------------------------------------------------------------------------
# schemas
# ---------------------------------------------------------------------------

READ_FILE_SCHEMA: dict[str, Any] = {
    "description": (
        "Read the content of a file in the IDE workspace. "
        "Optionally restrict to a line range with start_line / end_line "
        "(1-indexed, inclusive). Returns the file content as a string."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the IDE workspace root.",
            },
            "start_line": {
                "type": "integer",
                "description": "Optional 1-indexed start line (inclusive).",
            },
            "end_line": {
                "type": "integer",
                "description": "Optional 1-indexed end line (inclusive).",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

EDIT_FILE_SCHEMA: dict[str, Any] = {
    "description": (
        "Apply find-and-replace edits to a file using the IDE's native editor API. "
        "Each edit is an atomic operation: old_string is replaced by new_string. "
        "Multiple edits are applied in order. Edits appear in the IDE's native undo "
        "stack as a single atomic action."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the IDE workspace root.",
            },
            "edits": {
                "type": "array",
                "description": "Ordered list of find-and-replace edits.",
                "items": {
                    "type": "object",
                    "properties": {
                        "old_string": {
                            "type": "string",
                            "description": "Exact text to find and replace.",
                        },
                        "new_string": {
                            "type": "string",
                            "description": "Replacement text.",
                        },
                    },
                    "required": ["old_string", "new_string"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["path", "edits"],
        "additionalProperties": False,
    },
}

WRITE_FILE_SCHEMA: dict[str, Any] = {
    "description": (
        "Create a new file or overwrite an existing file in the IDE workspace. "
        "Creates parent directories if needed. The file is opened in the editor tab."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "File path relative to the IDE workspace root.",
            },
            "content": {
                "type": "string",
                "description": "Full file content.",
            },
        },
        "required": ["path", "content"],
        "additionalProperties": False,
    },
}

CREATE_DIRECTORY_SCHEMA: dict[str, Any] = {
    "description": (
        "Create a directory (and any missing parents) in the IDE workspace."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path relative to the IDE workspace root.",
            },
        },
        "required": ["path"],
        "additionalProperties": False,
    },
}

BROWSE_DIRECTORY_SCHEMA: dict[str, Any] = {
    "description": (
        "List the entries (files and subdirectories) in a directory. "
        "Returns entry names and types (file / directory)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Directory path relative to the IDE workspace root. "
                "Defaults to the workspace root when omitted.",
            },
        },
        "additionalProperties": False,
    },
}

SEARCH_CODE_SCHEMA: dict[str, Any] = {
    "description": (
        "Search file contents for a regex pattern (like grep / ripgrep). "
        "Returns matching file paths with line numbers and snippet context."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Regex pattern to search for inside file contents.",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in, relative to workspace root. "
                "Defaults to the entire workspace.",
            },
            "file_glob": {
                "type": "string",
                "description": "Optional glob to filter files (e.g. '*.py', '*.ts').",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
}

SEARCH_FILES_SCHEMA: dict[str, Any] = {
    "description": (
        "Find files by glob pattern (e.g. '*.py', '**/test_*.ts'). "
        "Returns matching file paths relative to the workspace root."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Glob pattern (supports ** for recursive matching).",
            },
            "path": {
                "type": "string",
                "description": "Directory to search in, relative to workspace root. "
                "Defaults to the workspace root.",
            },
        },
        "required": ["pattern"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def handle_read_file(
    path: str = "",
    start_line: int | None = None,
    end_line: int | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension reads the file locally."""
    if not path:
        return {"ok": False, "error": "path is required"}
    params: dict[str, Any] = {"path": path}
    if start_line is not None:
        params["start_line"] = start_line
    if end_line is not None:
        params["end_line"] = end_line
    return deferred("read_file", params)


def handle_edit_file(
    path: str = "",
    edits: list[dict[str, str]] | None = None,
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension applies edits via native editor API."""
    if not path:
        return {"ok": False, "error": "path is required"}
    if not edits:
        return {"ok": False, "error": "edits is required and must be non-empty"}
    return deferred("edit_file", {"path": path, "edits": edits})


def handle_write_file(
    path: str = "",
    content: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension creates/overwrites the file."""
    if not path:
        return {"ok": False, "error": "path is required"}
    # content may be empty (writing an empty file is valid)
    return deferred("write_file", {"path": path, "content": content})


def handle_create_directory(
    path: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension creates the directory."""
    if not path:
        return {"ok": False, "error": "path is required"}
    return deferred("create_directory", {"path": path})


def handle_browse_directory(
    path: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension lists the directory."""
    params: dict[str, Any] = {}
    if path:
        params["path"] = path
    return deferred("browse_directory", params)


def handle_search_code(
    pattern: str = "",
    path: str = "",
    file_glob: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension greps the workspace."""
    if not pattern:
        return {"ok": False, "error": "pattern is required"}
    params: dict[str, Any] = {"pattern": pattern}
    if path:
        params["path"] = path
    if file_glob:
        params["file_glob"] = file_glob
    return deferred("search_code", params)


def handle_search_files(
    pattern: str = "",
    path: str = "",
    **_: Any,
) -> dict[str, Any]:
    """Defer execution — the IDE extension glob-searches for files."""
    if not pattern:
        return {"ok": False, "error": "pattern is required"}
    params: dict[str, Any] = {"pattern": pattern}
    if path:
        params["path"] = path
    return deferred("search_files", params)
