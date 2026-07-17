"""Centralized tool-call firewall for the Hermes agent.

Implements all 11 guardrails as defined in the agent-security-rules product spec.
The firewall operates at the tool-call layer — between the LLM's tool_use block and
the handler's invocation — without modifying individual tool handlers.

Public API:
  check(tool_name, arguments, session_context=None) → (allowed: bool, reason_code: str | None)
  sanitize_result(tool_name, result_content) → sanitized_content
  build_refusal_message(reason_code, tool_name, guardrail_id) → dict
  HandledException — for LLM-visible refusals

Enable/disable via HERMES_GUARDRAILS_ENABLED env var (default: enabled).
Set to "0", "false", "no", or "off" to disable (debugging only; never in production).
"""

from __future__ import annotations

import logging
import os
import re
from enum import Enum
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _enabled() -> bool:
    return os.environ.get("HERMES_GUARDRAILS_ENABLED", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


# ---------------------------------------------------------------------------
# Reason codes
# ---------------------------------------------------------------------------


class ReasonCode(str, Enum):
    DELETION_BLOCKED = "deletion_blocked"
    SCRIPT_EXECUTION_BLOCKED = "script_execution_blocked"
    ENV_DISCLOSURE_BLOCKED = "env_disclosure_blocked"
    SYSTEM_INTROSPECTION_BLOCKED = "system_introspection_blocked"
    DOWNLOAD_BLOCKED = "download_blocked"
    TRANSITION_BLOCKED = "transition_blocked"
    PR_APPROVE_BLOCKED = "pr_approve_blocked"
    CONTENT_SANITIZATION_BLOCKED = "content_sanitization_blocked"
    CTA_PHISHING_BLOCKED = "cta_phishing_blocked"
    CROSS_WORKSPACE_BLOCKED = "cross_workspace_blocked"
    SYSTEM_PROMPT_SOURCE_BLOCKED = "system_prompt_source_blocked"


class HandledException(Exception):
    """Raised when the guardrail firewall blocks a tool call with a LLM-visible refusal."""

    def __init__(
        self, reason_code: str, tool_name: str, guardrail_id: str = ""
    ) -> None:
        self.reason_code = reason_code
        self.tool_name = tool_name
        self.guardrail_id = guardrail_id
        super().__init__(f"Guardrail blocked: {reason_code} (tool={tool_name})")


# ---------------------------------------------------------------------------
# Constants and compiled patterns
# ---------------------------------------------------------------------------

# G11 — protected system prompt source paths
SYSTEM_PROMPT_SOURCE_PATHS: frozenset[str] = frozenset(
    {
        "CLAUDE.md",
        "HERMES.md",
        "claude.md",
        "hermes.md",
    }
)

# G8 — XSS patterns (pre-dispatch: block writes containing these)
XSS_PATTERNS = [
    re.compile(r"<script\b[^>]*>", re.IGNORECASE),
    re.compile(r"\bjavascript\s*:", re.IGNORECASE),
    re.compile(r"\bon\w+\s*=\s*[\"'`]", re.IGNORECASE),  # onerror=, onload=, onclick=
    re.compile(r"<iframe\b", re.IGNORECASE),
    re.compile(r"<object\b", re.IGNORECASE),
    re.compile(r"<embed\b", re.IGNORECASE),
    re.compile(r"\bvbscript\s*:", re.IGNORECASE),
    re.compile(r"data\s*:\s*text/html\b", re.IGNORECASE),
]

# G7 — OOB marker pattern (post-dispatch: strip from tool results)
OOB_MARKER_PATTERN = re.compile(
    r"\[OUT-OF-BAND USER MESSAGE\b.*?\[/OUT-OF-BAND USER MESSAGE\]",
    re.DOTALL | re.IGNORECASE,
)

# G2 — shell execution blocked patterns (command argument)
SHELL_BLOCKED_PATTERNS = [
    re.compile(r"\bsh\s+-c\b", re.IGNORECASE),
    re.compile(r"\bbash\s+-c\b", re.IGNORECASE),
    re.compile(r"\bpython3?\s+-c\b", re.IGNORECASE),
    re.compile(r"\bnode\s+-e\b", re.IGNORECASE),
    re.compile(r"\bperl\s+-e\b", re.IGNORECASE),
    re.compile(r"\bruby\s+-e\b", re.IGNORECASE),
    re.compile(r"\bcurl\b.*\|\s*(sh|bash|zsh|ksh)\b", re.IGNORECASE),
    re.compile(r"\bwget\b.*\|\s*(sh|bash|zsh|ksh)\b", re.IGNORECASE),
    re.compile(r"\beval\s+[`\"']", re.IGNORECASE),
]

# G5 — web download blocked patterns (command argument)
DOWNLOAD_BLOCKED_PATTERNS = [
    re.compile(r"\bcurl\b.*-[oO]\b", re.IGNORECASE),
    re.compile(r"\bwget\b", re.IGNORECASE),
    re.compile(r"\bgit\s+clone\b", re.IGNORECASE),
    re.compile(r"\bpip\s+install\b.*https?://", re.IGNORECASE),
    re.compile(r"\bnpm\s+install\b.*https?://", re.IGNORECASE),
    re.compile(r"\bgo\s+get\b.*https?://", re.IGNORECASE),
    re.compile(r"\bcurl\b.*https?://.*>", re.IGNORECASE),  # curl url > file
]

# G3 — env disclosure patterns (path or command arguments)
ENV_DISCLOSURE_PATTERNS = [
    re.compile(r"\bprintenv\b", re.IGNORECASE),
    re.compile(r"(?:^|\s)env(?:\s|$)", re.IGNORECASE),  # `env` as standalone command
    re.compile(r"\becho\s+\$\w+", re.IGNORECASE),  # echo $VAR
    re.compile(r"cat\s+\.env\b", re.IGNORECASE),
    re.compile(r"~?/\.aws/", re.IGNORECASE),
    re.compile(r"~?/\.ssh/\w+", re.IGNORECASE),
    re.compile(r"\.pem\b", re.IGNORECASE),
    re.compile(r"\.key\b", re.IGNORECASE),
]

# G9 — lifecycle mutation patterns (suggest_next_actions action_text)
LIFECYCLE_ACTION_PATTERNS = [
    re.compile(r"\bapprove[_\s]feature\b", re.IGNORECASE),
    re.compile(r"\bcreate[_\s]tasks\b", re.IGNORECASE),
    re.compile(r"\bworkflow[_\s]init[_\s]feature\b", re.IGNORECASE),
    re.compile(r"\brequest[_\s]approval\b", re.IGNORECASE),
    # task-execution CTAs — owned by the orchestrator, not the human user
    re.compile(r"\bstart[_\s]task\b", re.IGNORECASE),
    re.compile(r"\bstart[_\s]implementation\b", re.IGNORECASE),
    re.compile(r"\brun[_\s]task\b", re.IGNORECASE),
]

# G4 — system introspection patterns (exposed for scope_guard reuse)
INTROSPECTION_PATTERNS = [
    re.compile(r"\bsystem\s+prompt\b", re.IGNORECASE),
    re.compile(r"\byour\s+instructions\b", re.IGNORECASE),
    re.compile(r"\byour\s+rules\b", re.IGNORECASE),
    re.compile(r"\brepeat\s+everything\s+above\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+tools\s+do\s+you\s+have\b", re.IGNORECASE),
    re.compile(
        r"\bshow\s+me\s+your\s+(?:tools|functions|prompt|instructions)\b", re.IGNORECASE
    ),
    re.compile(r"\bprint\s+your\s+(?:prompt|instructions|rules)\b", re.IGNORECASE),
    re.compile(r"\blist\s+(?:all\s+)?your\s+(?:tools|functions)\b", re.IGNORECASE),
    re.compile(r"\byour\s+architecture\b", re.IGNORECASE),
    re.compile(r"\bwhat\s+model\s+are\s+you\b", re.IGNORECASE),
    re.compile(r"\bignore\s+(?:all\s+)?previous\s+instructions\b", re.IGNORECASE),
]

# G1 — deletion tool name keywords (future-proof guard; substring match since _ is a word char)
_DELETION_KEYWORDS: frozenset[str] = frozenset(
    {
        "delete",
        "remove",
        "wipe",
        "truncate",
        "drop",
        "purge",
        "erase",
        "destroy",
    }
)

# Shell/terminal tool names (G2, G5 triggers)
_SHELL_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "terminal",
        "shell",
        "run_command",
        "execute_command",
        "bash",
        "sh",
        "run_bash",
        "run_shell",
    }
)

# Write tools subject to G8 (content sanitization) and G11 (system prompt protection)
_WRITE_TOOLS: frozenset[str] = frozenset(
    {
        "write_file",
        "edit_file",
        "write_product_spec",
        "write_technical_design",
        "write_tasks",
        "edit_document",
    }
)

# Read-path tools subject to G7 OOB stripping in sanitize_result
_READ_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "read_workspace_file",
        "github_pr_context",
        "query_rag",
        "query_gitnexus",
        "get_feature_state",
        "get_workspace_context",
        "get_tasks",
        "list_documents",
    }
)

# Guardrail display messages {reason_code: (guardrail_id, message)}
_GUARDRAIL_MESSAGES: dict[str, tuple[str, str]] = {
    ReasonCode.DELETION_BLOCKED: (
        "G1",
        "The agent cannot perform deletion or destructive operations. "
        "Data preservation is a hard requirement — no files, features, tasks, or "
        "workspace entities may be deleted.",
    ),
    ReasonCode.SCRIPT_EXECUTION_BLOCKED: (
        "G2",
        "The agent cannot execute arbitrary scripts or shell commands. "
        "Only read-only inspection commands within authorized workflow skills are permitted.",
    ),
    ReasonCode.ENV_DISCLOSURE_BLOCKED: (
        "G3",
        "The agent cannot disclose environment variables, API keys, credentials, or "
        "other secret material. Credentials are used internally but never surfaced.",
    ),
    ReasonCode.SYSTEM_INTROSPECTION_BLOCKED: (
        "G4",
        "The agent cannot describe its own system prompt, architecture, internal tool "
        "definitions, or runtime configuration. Its purpose is workspace software work.",
    ),
    ReasonCode.DOWNLOAD_BLOCKED: (
        "G5",
        "The agent cannot download, clone, or fetch source code or binaries from external "
        "URLs. Authorized tools (query_rag, query_gitnexus, github_pr_context) use "
        "controlled backends and are unaffected.",
    ),
    ReasonCode.TRANSITION_BLOCKED: (
        "G6",
        "The agent cannot advance the feature past the technical design stage without "
        "human authorization. Handoff approval and task cancellation require human action.",
    ),
    ReasonCode.PR_APPROVE_BLOCKED: (
        "G6",
        "The agent cannot post an APPROVE review on a GitHub PR without explicit human "
        "confirmation. REQUEST_CHANGES reviews and read-only PR operations are allowed.",
    ),
    ReasonCode.CONTENT_SANITIZATION_BLOCKED: (
        "G8",
        "The agent cannot write content containing executable web content (script tags, "
        "javascript: URLs, event handlers). Documents must be safe for all workspace members.",
    ),
    ReasonCode.CTA_PHISHING_BLOCKED: (
        "G9",
        "The agent cannot suggest actions whose text would trigger lifecycle mutations "
        "(approve_feature, create_tasks, etc.). CTAs must be safe for users to click.",
    ),
    ReasonCode.CROSS_WORKSPACE_BLOCKED: (
        "G10",
        "The agent cannot access data from a workspace other than the session's bound "
        "workspace. Cross-workspace access requires explicit human authorization.",
    ),
    ReasonCode.SYSTEM_PROMPT_SOURCE_BLOCKED: (
        "G11",
        "The agent cannot modify CLAUDE.md, HERMES.md, or any file that is used as a "
        "system prompt source. These files are managed by administrators only.",
    ),
}


# ---------------------------------------------------------------------------
# Per-guardrail check functions
# ---------------------------------------------------------------------------


def _check_G1_deletion(tool_name: str) -> Optional[str]:
    """G1 — Block tools whose names indicate destructive deletion operations.

    Uses substring matching because `_` is a word character in Python regex,
    so `\bdelete\b` would not match `delete_file`. Substring is intentionally
    broad — any future tool containing these keywords is blocked.
    """
    lower = tool_name.lower()
    for kw in _DELETION_KEYWORDS:
        if kw in lower:
            return ReasonCode.DELETION_BLOCKED
    return None


def _check_G2_script(tool_name: str, arguments: dict[str, Any]) -> Optional[str]:
    """G2 — Block arbitrary script and shell execution via terminal-class tools."""
    if tool_name not in _SHELL_TOOL_NAMES:
        return None
    command = str(arguments.get("command", ""))
    for pattern in SHELL_BLOCKED_PATTERNS:
        if pattern.search(command):
            return ReasonCode.SCRIPT_EXECUTION_BLOCKED
    return None


def _check_G3_env_disclosure(
    tool_name: str, arguments: dict[str, Any]
) -> Optional[str]:
    """G3 — Block env variable and credential disclosure.

    For shell tools: scan the command argument.
    For all tools: scan path arguments for protected credential file references.
    """
    # Shell tool: scan command argument for env disclosure patterns
    if tool_name in _SHELL_TOOL_NAMES:
        command = str(arguments.get("command", ""))
        for pattern in ENV_DISCLOSURE_PATTERNS:
            if pattern.search(command):
                return ReasonCode.ENV_DISCLOSURE_BLOCKED

    # All tools: block reads/writes that target credential files by path
    path = arguments.get("path", "") or arguments.get("filename", "") or ""
    if path:
        path_str = str(path)
        # Block .env file access
        if re.search(r"(?:^|/)\.env(?:\.|$)", path_str, re.IGNORECASE):
            return ReasonCode.ENV_DISCLOSURE_BLOCKED
        # Block SSH private key files
        if re.search(r"/\.ssh/(?!known_hosts|config)", path_str, re.IGNORECASE):
            return ReasonCode.ENV_DISCLOSURE_BLOCKED
        # Block AWS credential files
        if re.search(r"/\.aws/", path_str, re.IGNORECASE):
            return ReasonCode.ENV_DISCLOSURE_BLOCKED
        # Block PEM/key files
        if re.search(r"\.(pem|key|p12|pfx|crt|cer)$", path_str, re.IGNORECASE):
            return ReasonCode.ENV_DISCLOSURE_BLOCKED

    return None


def _check_G5_downloads(tool_name: str, arguments: dict[str, Any]) -> Optional[str]:
    """G5 — Block web downloads and external source retrieval via shell tools."""
    if tool_name not in _SHELL_TOOL_NAMES:
        return None
    command = str(arguments.get("command", ""))
    for pattern in DOWNLOAD_BLOCKED_PATTERNS:
        if pattern.search(command):
            return ReasonCode.DOWNLOAD_BLOCKED
    return None


def _check_G6_transitions(tool_name: str, arguments: dict[str, Any]) -> Optional[str]:
    """G6 — Block invalid lifecycle and task state transitions.

    - approve_feature(stage="handoff") → transition_blocked
    - github_pr_review(event="APPROVE") → pr_approve_blocked
    """
    if tool_name == "approve_feature":
        stage = str(arguments.get("stage", "")).lower()
        action = str(arguments.get("action", "approve")).lower()
        if stage == "handoff" and action == "approve":
            return ReasonCode.TRANSITION_BLOCKED

    if tool_name == "github_pr_review":
        event = str(arguments.get("event", "")).upper()
        if event == "APPROVE":
            return ReasonCode.PR_APPROVE_BLOCKED

    return None


def _check_G8_xss(tool_name: str, arguments: dict[str, Any]) -> Optional[str]:
    """G8 — Block XSS content in write/edit operations."""
    if tool_name not in _WRITE_TOOLS:
        return None

    # Collect all content strings from arguments
    content_strings: list[str] = []

    # Direct content field (write_file, write_product_spec, etc.)
    content = arguments.get("content")
    if isinstance(content, str):
        content_strings.append(content)

    # edits list (edit_file, edit_document)
    edits = arguments.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                new_str = edit.get("new_string", "")
                if isinstance(new_str, str):
                    content_strings.append(new_str)

    for text in content_strings:
        for pattern in XSS_PATTERNS:
            if pattern.search(text):
                return ReasonCode.CONTENT_SANITIZATION_BLOCKED

    return None


def _check_G9_cta_phishing(tool_name: str, arguments: dict[str, Any]) -> Optional[str]:
    """G9 — Block lifecycle-mutation CTAs in suggest_next_actions."""
    if tool_name != "suggest_next_actions":
        return None

    suggestions = arguments.get("suggestions", [])
    if not isinstance(suggestions, list):
        return None

    for suggestion in suggestions:
        if not isinstance(suggestion, dict):
            continue
        action_text = str(suggestion.get("action_text", ""))
        for pattern in LIFECYCLE_ACTION_PATTERNS:
            if pattern.search(action_text):
                return ReasonCode.CTA_PHISHING_BLOCKED

    return None


def _check_G10_workspace(
    tool_name: str,
    arguments: dict[str, Any],
    session_context: Optional[dict[str, Any]],
) -> Optional[str]:
    """G10 — Enforce agent-side workspace membership gate.

    If a tool passes a workspace_id that differs from the session's bound workspace,
    block the call.
    """
    # Resolve session workspace_id — prefer explicit session_context, fall back to thread-local
    session_workspace_id = ""
    if session_context and isinstance(session_context, dict):
        session_workspace_id = str(session_context.get("workspace_id", "") or "")

    if not session_workspace_id:
        # Try thread-local fallback
        try:
            from plugins.context import get_workspace_id

            session_workspace_id = get_workspace_id() or ""
        except Exception:
            pass

    # No session workspace set — skip check (general chat without workspace context)
    if not session_workspace_id:
        return None

    arg_workspace_id = arguments.get("workspace_id")
    if arg_workspace_id and str(arg_workspace_id) != session_workspace_id:
        return ReasonCode.CROSS_WORKSPACE_BLOCKED

    return None


def _check_G11_system_prompt(
    tool_name: str, arguments: dict[str, Any]
) -> Optional[str]:
    """G11 — Block modification of system prompt source files (CLAUDE.md, HERMES.md)."""
    if tool_name not in _WRITE_TOOLS:
        return None

    path = arguments.get("path", "") or ""
    if not path:
        return None

    # Normalize to just the filename component for comparison
    path_str = str(path).replace("\\", "/")
    filename = path_str.split("/")[-1] if "/" in path_str else path_str

    if filename in SYSTEM_PROMPT_SOURCE_PATHS or path_str in SYSTEM_PROMPT_SOURCE_PATHS:
        return ReasonCode.SYSTEM_PROMPT_SOURCE_BLOCKED

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check(
    tool_name: str,
    arguments: Optional[dict[str, Any]] = None,
    session_context: Optional[dict[str, Any]] = None,
) -> tuple[bool, Optional[str]]:
    """Pre-dispatch guardrail gate.

    Returns (allowed, reason_code). When allowed is False, reason_code identifies
    which guardrail was triggered. When allowed is True, reason_code is None.

    Fails closed: any exception inside a guardrail check is logged and the call
    is blocked rather than silently permitted.
    """
    if not _enabled():
        return True, None

    if arguments is None:
        arguments = {}

    # Each check returns a reason_code string on block, or None to allow.
    checks = [
        lambda: _check_G1_deletion(tool_name),
        lambda: _check_G2_script(tool_name, arguments),
        lambda: _check_G3_env_disclosure(tool_name, arguments),
        lambda: _check_G5_downloads(tool_name, arguments),
        lambda: _check_G6_transitions(tool_name, arguments),
        lambda: _check_G8_xss(tool_name, arguments),
        lambda: _check_G9_cta_phishing(tool_name, arguments),
        lambda: _check_G10_workspace(tool_name, arguments, session_context),
        lambda: _check_G11_system_prompt(tool_name, arguments),
    ]

    for guardrail_fn in checks:
        try:
            reason_code = guardrail_fn()
        except Exception as exc:
            # Fail closed: unexpected errors block the call
            logger.error(
                "guardrail check raised unexpectedly (fail-closed): tool=%s exc=%s",
                tool_name,
                exc,
                exc_info=True,
            )
            return (
                False,
                ReasonCode.DELETION_BLOCKED,
            )  # generic block on unexpected error

        if reason_code is not None:
            logger.info(
                "guardrail blocked: tool=%s reason=%s",
                tool_name,
                reason_code,
            )
            return False, reason_code

    return True, None


def sanitize_result(tool_name: str, result_content: Any) -> Any:
    """Post-dispatch result sanitizer (G7).

    Strips OOB injection markers from tool results before they reach the LLM.
    Applies to all read-path tools and MCP responses (query_rag, query_gitnexus).

    Returns the sanitized content in the same type as the input.
    """
    if not _enabled():
        return result_content

    # All tools get OOB marker sanitization — an attacker can embed markers
    # in any data source the agent reads.
    if isinstance(result_content, str):
        return _strip_oob_markers(result_content)

    if isinstance(result_content, dict):
        return _sanitize_dict(result_content)

    if isinstance(result_content, list):
        return [sanitize_result(tool_name, item) for item in result_content]

    return result_content


def _strip_oob_markers(text: str) -> str:
    """Remove all OOB injection markers from a text string."""
    return OOB_MARKER_PATTERN.sub("[content removed by security filter]", text)


def _sanitize_dict(data: dict[str, Any]) -> dict[str, Any]:
    """Recursively sanitize string values in a dict."""
    result: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, str):
            result[key] = _strip_oob_markers(value)
        elif isinstance(value, dict):
            result[key] = _sanitize_dict(value)
        elif isinstance(value, list):
            result[key] = [
                _strip_oob_markers(v)
                if isinstance(v, str)
                else _sanitize_dict(v)
                if isinstance(v, dict)
                else v
                for v in value
            ]
        else:
            result[key] = value
    return result


def build_refusal_message(
    reason_code: str,
    tool_name: str,
    guardrail_id: str = "",
) -> dict[str, Any]:
    """Build a structured refusal message to return as a tool_result to the LLM.

    The LLM receives this as the tool's output and can communicate the refusal
    naturally to the user. Returning as tool_result (not raising an exception)
    keeps the conversation loop intact.
    """
    gid, message = _GUARDRAIL_MESSAGES.get(
        reason_code,
        ("G?", f"Guardrail blocked tool invocation: {reason_code}."),
    )
    if not guardrail_id:
        guardrail_id = gid

    return {
        "ok": False,
        "error": f"Guardrail blocked: {reason_code}",
        "reason_code": reason_code,
        "message": message,
        "tool": tool_name,
        "guardrail": guardrail_id,
    }


def check_introspection(message: str) -> bool:
    """Check if a user message attempts system introspection (G4).

    Returns True if the message matches introspection patterns and should be blocked.
    Used by scope_guard to gate pre-turn messages.
    """
    if not _enabled():
        return False
    text = (message or "").strip()
    if not text:
        return False
    for pattern in INTROSPECTION_PATTERNS:
        if pattern.search(text):
            logger.info(
                "guardrail G4: system introspection attempt detected: %r", text[:120]
            )
            return True
    return False
