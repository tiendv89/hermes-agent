"""Document commit pipeline for hermes-agent.

Implements read-before-write + feature-branch commit + create/update PR.
One write path shared by agent tools and the human-save endpoint.

All calls use GITHUB_TOKEN against the management repo via the GitHub REST API.
"""

from __future__ import annotations

import base64
import logging
import re
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 30
_GITHUB_API_URL = "https://api.github.com"

# Patterns for branch and feature-id validation
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")

# GitHub signals a SHA conflict via 409 or 422 with a message containing "sha"
_SHA_MISMATCH_MSGS = ("does not match", "sha", "conflict")


class StaleBaseError(Exception):
    """Raised when the document's SHA changed since we last read it.

    The caller should reload the document and retry (or surface as a 409).
    """

    def __init__(self, path: str, detail: str = ""):
        self.path = path
        self.detail = detail
        super().__init__(f"Stale base SHA for {path!r}: {detail}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
        "Content-Type": "application/json",
    }


def _branch_ref(feature_id: str) -> str:
    return f"feature/{feature_id}"


def _validate_feature_id(feature_id: str) -> None:
    if not _SAFE_ID_RE.match(feature_id):
        raise ValueError(
            f"Invalid feature_id {feature_id!r}: only alphanumerics, hyphens, and underscores are allowed."
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_branch(
    owner: str,
    repo: str,
    branch: str,
    base_branch: str,
    token: str,
) -> None:
    """Create ``branch`` from ``base_branch`` if it does not exist (generic).

    Unlike ensure_feature_branch this takes the full branch name, so it works
    for the init branch (``feature/<slug>-init``) too. No-ops when present.
    """
    ref_url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/git/refs/heads/{branch}"
    check = requests.get(ref_url, headers=_headers(token), timeout=_DEFAULT_TIMEOUT)
    if check.status_code == 200:
        return
    if check.status_code != 404:
        check.raise_for_status()
    base_url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/git/refs/heads/{base_branch}"
    base_resp = requests.get(base_url, headers=_headers(token), timeout=_DEFAULT_TIMEOUT)
    base_resp.raise_for_status()
    base_sha = base_resp.json()["object"]["sha"]
    create_resp = requests.post(
        f"{_GITHUB_API_URL}/repos/{owner}/{repo}/git/refs",
        headers=_headers(token),
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        timeout=_DEFAULT_TIMEOUT,
    )
    if create_resp.status_code not in (201, 422):
        create_resp.raise_for_status()
    logger.info("ensure_branch: created %s from %s (%s)", branch, base_branch, base_sha[:7])


def ensure_pr_for_head(
    owner: str,
    repo: str,
    head_branch: str,
    base_branch: str,
    token: str,
    title: str,
    body: str = "",
) -> Dict[str, Any]:
    """Find an open PR for ``head_branch`` → ``base_branch`` or create one.

    Generic over the head branch (used for the init branch). Returns
    ``{number, url, state}``.
    """
    list_url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/pulls"
    resp = requests.get(
        list_url,
        headers=_headers(token),
        params={"state": "open", "head": f"{owner}:{head_branch}", "base": base_branch},
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    pulls = resp.json()
    if pulls:
        pr = pulls[0]
        return {"number": pr["number"], "url": pr["html_url"], "state": pr["state"]}

    create_resp = requests.post(
        list_url,
        headers=_headers(token),
        json={"title": title, "head": head_branch, "base": base_branch, "body": body},
        timeout=_DEFAULT_TIMEOUT,
    )
    if create_resp.status_code == 422:
        resp2 = requests.get(
            list_url,
            headers=_headers(token),
            params={"state": "open", "head": f"{owner}:{head_branch}", "base": base_branch},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp2.raise_for_status()
        existing = resp2.json()
        if existing:
            pr = existing[0]
            return {"number": pr["number"], "url": pr["html_url"], "state": pr["state"]}
    create_resp.raise_for_status()
    pr = create_resp.json()
    return {"number": pr["number"], "url": pr["html_url"], "state": pr["state"]}


def ensure_feature_branch(
    owner: str,
    repo: str,
    feature_id: str,
    base_branch: str,
    token: str,
) -> None:
    """Create ``feature/<feature_id>`` from ``base_branch`` if it does not exist.

    Uses the Git Refs API. No-ops when the branch already exists.
    """
    _validate_feature_id(feature_id)
    branch = _branch_ref(feature_id)
    ref_url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/git/refs/heads/{branch}"
    check = requests.get(ref_url, headers=_headers(token), timeout=_DEFAULT_TIMEOUT)
    if check.status_code == 200:
        return  # branch already exists
    if check.status_code != 404:
        check.raise_for_status()

    # Branch is absent — get the base branch SHA and create from it.
    base_url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/git/refs/heads/{base_branch}"
    base_resp = requests.get(
        base_url, headers=_headers(token), timeout=_DEFAULT_TIMEOUT
    )
    base_resp.raise_for_status()
    base_sha = base_resp.json()["object"]["sha"]

    create_url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/git/refs"
    create_resp = requests.post(
        create_url,
        headers=_headers(token),
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
        timeout=_DEFAULT_TIMEOUT,
    )
    # 422 "Reference already exists" — lost a race with another writer; fine.
    if create_resp.status_code not in (201, 422):
        create_resp.raise_for_status()
    logger.info(
        "ensure_feature_branch: created %s from %s (%s)",
        branch,
        base_branch,
        base_sha[:7],
    )


def read_document(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    token: str,
) -> Dict[str, Any]:
    """Return ``{content: str, sha: str|None}`` for the file at ``path`` on ``branch``.

    Returns ``{content: '', sha: None}`` when the file does not exist (404).
    """
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
    resp = requests.get(
        url,
        headers=_headers(token),
        params={"ref": branch},
        timeout=_DEFAULT_TIMEOUT,
    )
    if resp.status_code == 404:
        return {"content": "", "sha": None}
    resp.raise_for_status()
    data = resp.json()
    raw = base64.b64decode(data["content"]).decode("utf-8")
    return {"content": raw, "sha": data["sha"]}


def write_document(
    owner: str,
    repo: str,
    feature_id: str,
    base_branch: str,
    path: str,
    content: str,
    base_sha: Optional[str],
    message: str,
    token: str,
) -> Dict[str, Any]:
    """Commit ``content`` to ``feature/<feature_id>`` at ``path``.

    Steps:
    1. ``ensure_feature_branch`` — create the branch if absent.
    2. PUT to GitHub Contents API with ``branch`` + ``sha=base_sha``.
       GitHub rejects stale SHAs with HTTP 409 or 422 → raises ``StaleBaseError``.
    3. ``ensure_pr`` — create or retrieve the feature's tracked PR.

    Returns ``{commit_sha, pr}``.
    """
    _validate_feature_id(feature_id)
    branch = _branch_ref(feature_id)
    ensure_feature_branch(owner, repo, feature_id, base_branch, token)

    payload: Dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if base_sha is not None:
        payload["sha"] = base_sha

    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
    resp = requests.put(
        url, headers=_headers(token), json=payload, timeout=_DEFAULT_TIMEOUT
    )

    if resp.status_code in (409, 422):
        detail = ""
        try:
            detail = resp.json().get("message", "")
        except Exception:
            pass
        raise StaleBaseError(path, detail)

    resp.raise_for_status()
    commit_sha = resp.json().get("commit", {}).get("sha", "")
    pr = ensure_pr(owner, repo, feature_id, base_branch, token)
    return {"commit_sha": commit_sha, "pr": pr}


def branch_exists(
    owner: str,
    repo: str,
    branch: str,
    token: str,
) -> bool:
    """Return True if *branch* exists on the remote, False if 404."""
    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/git/refs/heads/{branch}"
    resp = requests.get(url, headers=_headers(token), timeout=_DEFAULT_TIMEOUT)
    if resp.status_code == 200:
        return True
    if resp.status_code == 404:
        return False
    resp.raise_for_status()
    return False  # unreachable


def commit_to_branch(
    owner: str,
    repo: str,
    branch: str,
    path: str,
    content: str,
    base_sha: Optional[str],
    message: str,
    token: str,
) -> str:
    """Commit *content* directly to *branch* at *path* and return the commit SHA.

    Unlike write_document, this function does NOT call ensure_feature_branch or
    ensure_pr — it is used when the caller has already resolved the target branch
    (e.g. an existing init PR branch) and wants a bare commit.

    Raises StaleBaseError on 409/422 SHA mismatch.
    """
    payload: Dict[str, Any] = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if base_sha is not None:
        payload["sha"] = base_sha

    url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/contents/{path}"
    resp = requests.put(
        url, headers=_headers(token), json=payload, timeout=_DEFAULT_TIMEOUT
    )

    if resp.status_code in (409, 422):
        detail = ""
        try:
            detail = resp.json().get("message", "")
        except Exception:
            pass
        raise StaleBaseError(path, detail)

    resp.raise_for_status()
    return resp.json().get("commit", {}).get("sha", "")


def commit_files(
    owner: str,
    repo: str,
    branch: str,
    files: Dict[str, str],
    message: str,
    token: str,
) -> str:
    """Commit multiple files to *branch* in a single commit (Git Data API).

    Used to scaffold a feature (status.yaml + templates) in one commit. The
    branch must already exist. Returns the new commit SHA.
    """
    api = _GITHUB_API_URL
    ref = requests.get(
        f"{api}/repos/{owner}/{repo}/git/refs/heads/{branch}",
        headers=_headers(token), timeout=_DEFAULT_TIMEOUT,
    )
    ref.raise_for_status()
    base_sha = ref.json()["object"]["sha"]

    commit_obj = requests.get(
        f"{api}/repos/{owner}/{repo}/git/commits/{base_sha}",
        headers=_headers(token), timeout=_DEFAULT_TIMEOUT,
    )
    commit_obj.raise_for_status()
    base_tree = commit_obj.json()["tree"]["sha"]

    tree_entries = []
    for path, content in files.items():
        blob = requests.post(
            f"{api}/repos/{owner}/{repo}/git/blobs",
            headers=_headers(token),
            json={"content": base64.b64encode(content.encode("utf-8")).decode("ascii"), "encoding": "base64"},
            timeout=_DEFAULT_TIMEOUT,
        )
        blob.raise_for_status()
        tree_entries.append({"path": path.lstrip("/"), "mode": "100644", "type": "blob", "sha": blob.json()["sha"]})

    tree = requests.post(
        f"{api}/repos/{owner}/{repo}/git/trees",
        headers=_headers(token),
        json={"base_tree": base_tree, "tree": tree_entries},
        timeout=_DEFAULT_TIMEOUT,
    )
    tree.raise_for_status()

    commit = requests.post(
        f"{api}/repos/{owner}/{repo}/git/commits",
        headers=_headers(token),
        json={"message": message, "tree": tree.json()["sha"], "parents": [base_sha]},
        timeout=_DEFAULT_TIMEOUT,
    )
    commit.raise_for_status()
    new_sha = commit.json()["sha"]

    upd = requests.patch(
        f"{api}/repos/{owner}/{repo}/git/refs/heads/{branch}",
        headers=_headers(token),
        json={"sha": new_sha, "force": False},
        timeout=_DEFAULT_TIMEOUT,
    )
    upd.raise_for_status()
    return new_sha


def ensure_pr(
    owner: str,
    repo: str,
    feature_id: str,
    base_branch: str,
    token: str,
) -> Dict[str, Any]:
    """Find an open PR for ``feature/<feature_id>`` → ``base_branch`` or create one.

    One PR per feature — the PR carries both product spec and technical design.
    Returns the PR object with at least ``{url, number, state}``.
    """
    _validate_feature_id(feature_id)
    branch = _branch_ref(feature_id)

    # Look for an existing open PR from the feature branch.
    list_url = f"{_GITHUB_API_URL}/repos/{owner}/{repo}/pulls"
    resp = requests.get(
        list_url,
        headers=_headers(token),
        params={"state": "open", "head": f"{owner}:{branch}", "base": base_branch},
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    pulls = resp.json()
    if pulls:
        pr = pulls[0]
        return {
            "number": pr["number"],
            "url": pr["html_url"],
            "state": pr["state"],
        }

    # No open PR — create one.
    create_resp = requests.post(
        list_url,
        headers=_headers(token),
        json={
            "title": f"docs({feature_id}): product spec + technical design",
            "head": branch,
            "base": base_branch,
            "body": (
                f"Conversational authoring PR for feature `{feature_id}`.\n\n"
                "Contains `product-spec.md` and `technical-design.md` committed\n"
                "from the hermes-agent document pipeline (m3-agent-chat-v3).\n"
            ),
        },
        timeout=_DEFAULT_TIMEOUT,
    )
    # 422 "head branch already exists" or similar — list again.
    if create_resp.status_code == 422:
        resp2 = requests.get(
            list_url,
            headers=_headers(token),
            params={"state": "open", "head": f"{owner}:{branch}", "base": base_branch},
            timeout=_DEFAULT_TIMEOUT,
        )
        resp2.raise_for_status()
        existing = resp2.json()
        if existing:
            pr = existing[0]
            return {"number": pr["number"], "url": pr["html_url"], "state": pr["state"]}

    create_resp.raise_for_status()
    pr = create_resp.json()
    return {"number": pr["number"], "url": pr["html_url"], "state": pr["state"]}
