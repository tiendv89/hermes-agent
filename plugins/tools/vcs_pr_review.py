"""vcs_pr_review tool — post a PR review via vcs-service's two-call pattern (G3).

vcs-service's POST /api/vcs/pr/review_and_comment implements the two-call
posting pattern server-side (per product-spec §Step 6 / technical-design
§4):

  Step 6a — POST the full narrative to /issues/{n}/comments (always attempted;
             fatal on any failure).
  Step 6b — POST /pulls/{n}/reviews (skip gracefully on HTTP 422 self-review
             restriction; fatal on any other error).

Returns:
  {ok: True, review_url: <str>, self_review_skipped: <bool>}

  When step 6b hits GitHub's self-review restriction, ``review_url`` is the
  step-6a comment URL and ``self_review_skipped`` is True. When step 6b
  succeeds, ``review_url`` is the formal review URL and
  ``self_review_skipped`` is False.

Proxies through vcs-service (no GITHUB_TOKEN / local GitHub PAT needed).
Gated on VCS_SERVICE_URL/VCS_SERVICE_TOKEN presence, same convention as
``vcs_pr_context.py``.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_EVENTS = ("APPROVE", "REQUEST_CHANGES")

SCHEMA: Dict[str, Any] = {
    "description": (
        "Post a GitHub PR review using the two-call pattern: first posts the full "
        "narrative as an issue comment (always visible, not subject to self-review "
        "restriction), then posts a formal review event (APPROVE or REQUEST_CHANGES). "
        "If GitHub returns HTTP 422 on the review event (self-review restriction), the "
        "tool succeeds with self_review_skipped=true — the issue comment is the "
        "authoritative narrative. Use after vcs_pr_context to gather context and "
        "after reasoning against review_criteria."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "pr_url": {
                "type": "string",
                "description": (
                    "GitHub PR URL: https://github.com/{owner}/{repo}/pull/{number}."
                ),
            },
            "event": {
                "type": "string",
                "enum": list(_EVENTS),
                "description": (
                    "Review verdict: APPROVE (all findings are nits/optional) or "
                    "REQUEST_CHANGES (one or more blocking findings)."
                ),
            },
            "body": {
                "type": "string",
                "description": (
                    "Full review narrative — verdict, all findings with severity markers "
                    "(🔴 Blocker, 🟡 Warning, 🟢 Nit), inline references to files/lines. "
                    "Posted verbatim as the issue comment (step 6a) and as the review "
                    "event body (step 6b)."
                ),
            },
            "comments": {
                "type": "array",
                "description": (
                    "Optional inline review comments attached to the formal review event "
                    "(step 6b). Each entry targets a specific file and line in the diff."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to the repo root.",
                        },
                        "line": {
                            "type": "integer",
                            "description": "Line number in the diff to attach the comment to.",
                        },
                        "body": {
                            "type": "string",
                            "description": (
                                "Comment text. Include a severity marker prefix: "
                                "'🔴 **Blocker** — …', '🟡 **Warning** — …', or "
                                "'🟢 **Nit** — …'."
                            ),
                        },
                    },
                    "required": ["path", "line", "body"],
                    "additionalProperties": False,
                },
            },
        },
        "required": ["pr_url", "event", "body"],
        "additionalProperties": False,
    },
}


def check_available(**_: Any) -> bool:
    from src.services.vcs_service_client import check_vcs_service_available

    return check_vcs_service_available()


def _parse_pr_url(pr_url: str):
    import re

    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)", pr_url.strip())
    if not m:
        raise ValueError(
            f"Invalid GitHub PR URL {pr_url!r}. "
            "Expected https://github.com/{{owner}}/{{repo}}/pull/{{number}}."
        )
    return m.group(1), m.group(2), int(m.group(3))


def handle(
    pr_url: str = "",
    event: str = "",
    body: str = "",
    comments: Optional[List[Dict[str, Any]]] = None,
    **_: Any,
) -> Dict[str, Any]:
    from src.services.vcs_service_client import VCSServiceError, review_and_comment, run_async

    if not pr_url:
        return {"ok": False, "error": "pr_url is required."}
    if event not in _EVENTS:
        return {
            "ok": False,
            "error": f"Unknown event {event!r}. Expected one of: {', '.join(_EVENTS)}.",
        }
    if not body:
        return {"ok": False, "error": "body is required."}

    try:
        owner, repo, pull_number = _parse_pr_url(pr_url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    try:
        data = run_async(
            review_and_comment(
                owner, repo, pull_number, body, event, comments=comments or []
            )
        )
    except VCSServiceError as exc:
        logger.warning("vcs_pr_review: vcs-service error: %s", exc)
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        logger.warning("vcs_pr_review: unexpected error: %s", exc)
        return {"ok": False, "error": str(exc)}

    self_review_skipped = bool(data.get("self_review_skipped"))
    if self_review_skipped:
        logger.info(
            "vcs_pr_review: self-review restriction — pr=%s owner=%s repo=%s pull_number=%s",
            pr_url,
            owner,
            repo,
            pull_number,
        )
        print(
            f"reviewer_self_review_skipped pr_url={pr_url} "
            f"owner={owner} repo={repo} pull_number={pull_number}"
        )

    return {
        "ok": True,
        "review_url": data.get("review_url", ""),
        "self_review_skipped": self_review_skipped,
    }
