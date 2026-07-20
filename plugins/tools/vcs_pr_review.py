"""github_pr_review tool — post a PR review via the two-call pattern (G3).

Two-call posting pattern (per product-spec §Step 6 / technical-design §4):

  Step 6a — POST the full narrative to the issue comment endpoint (always
             attempted; fatal on any non-422 failure).
  Step 6b — POST the formal review event (skip gracefully on HTTP 422
             self-review restriction; fatal on any other error).

Returns:
  {ok: True, review_url: <str>, self_review_skipped: <bool>}

  When step 6b returns HTTP 422, ``review_url`` is the step-6a comment URL and
  ``self_review_skipped`` is True.  When step 6b succeeds (HTTP 201),
  ``review_url`` is the formal review URL and ``self_review_skipped`` is False.

All operations route through vcs-service proxy endpoints — no direct
GitHub API calls.  Gated on ``VCS_SERVICE_URL`` presence.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

import requests

from plugins.clients.vcs_client import (
    parse_pr_url,
    post_issue_comment,
    post_pr_review,
)

logger = logging.getLogger(__name__)

_EVENTS = ("APPROVE", "REQUEST_CHANGES")

SCHEMA: Dict[str, Any] = {
    "description": (
        "Post a GitHub PR review using the two-call pattern: first posts the full "
        "narrative as an issue comment (always visible, not subject to self-review "
        "restriction), then posts a formal review event (APPROVE or REQUEST_CHANGES). "
        "If GitHub returns HTTP 422 on the review event (self-review restriction), the "
        "tool succeeds with self_review_skipped=true — the issue comment is the "
        "authoritative narrative. Use after github_pr_context to gather context and "
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
    """Return True only when VCS_SERVICE_URL is configured."""
    return bool(os.environ.get("VCS_SERVICE_URL", "").strip())


def handle(
    pr_url: str = "",
    event: str = "",
    body: str = "",
    comments: Optional[List[Dict[str, Any]]] = None,
    **_: Any,
) -> Dict[str, Any]:
    if not pr_url:
        return {"ok": False, "error": "pr_url is required."}
    if event not in _EVENTS:
        return {
            "ok": False,
            "error": f"Unknown event {event!r}. Expected one of: {', '.join(_EVENTS)}.",
        }
    if not body:
        return {"ok": False, "error": "body is required."}

    if not os.environ.get("VCS_SERVICE_URL", "").strip():
        return {"ok": False, "error": "VCS_SERVICE_URL is not configured."}

    try:
        owner, repo, pull_number = parse_pr_url(pr_url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Step 6a — Post issue comment (always attempted; fatal on non-422)
    # ------------------------------------------------------------------
    try:
        comment_data = post_issue_comment(owner, repo, pull_number, body)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        logger.warning(
            "github_pr_review step-6a issue comment failed: HTTP %s — %s",
            status,
            exc,
        )
        return {
            "ok": False,
            "error": (
                f"Failed to post issue comment (step 6a): HTTP {status}. Detail: {exc}"
            ),
        }
    except Exception as exc:
        logger.warning("github_pr_review step-6a unexpected error: %s", exc)
        return {"ok": False, "error": f"Failed to post issue comment (step 6a): {exc}"}

    comment_url: str = comment_data.get("html_url", "")

    # ------------------------------------------------------------------
    # Step 6b — Post formal review event (skip gracefully on HTTP 422)
    # ------------------------------------------------------------------
    try:
        review_resp = post_pr_review(
            owner,
            repo,
            pull_number,
            event=event,
            body=body,
            comments=comments or [],
        )
    except Exception as exc:
        logger.warning("github_pr_review step-6b unexpected error: %s", exc)
        return {
            "ok": False,
            "error": f"Failed to post review event (step 6b): {exc}",
        }

    if review_resp.status_code == 201:
        review_data = review_resp.json()
        review_url: str = review_data.get("html_url", "")
        return {
            "ok": True,
            "review_url": review_url,
            "self_review_skipped": False,
        }

    if review_resp.status_code == 422:
        logger.info(
            "github_pr_review step-6b: HTTP 422 self-review restriction — "
            "pr=%s owner=%s repo=%s pull_number=%s",
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
            "review_url": comment_url,
            "self_review_skipped": True,
        }

    # Any other non-201 status is fatal.
    logger.warning(
        "github_pr_review step-6b failed: HTTP %s — %s",
        review_resp.status_code,
        review_resp.text[:500],
    )
    return {
        "ok": False,
        "error": (
            f"Failed to post review event (step 6b): HTTP {review_resp.status_code}. "
            f"Detail: {review_resp.text[:500]}"
        ),
    }
