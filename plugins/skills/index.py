"""Skill index — GitHub API fetch implementation (OQ5 resolution).

OQ5 Decision: Fetch from GitHub API at startup
-----------------------------------------------
Skills live in the ``agent-workflow`` repo, not in hermes. Rather than bundle
a static snapshot (requires image rebuild on every skills update), hermes
fetches the ``claude/`` tree from GitHub once at startup and caches it in
memory. This uses the same ``GITHUB_TOKEN`` already required for document write
operations, and keeps skills current without container rebuilds.

Graceful degradation: if ``GITHUB_TOKEN`` or ``WORKFLOW_GITHUB_REPO`` are
absent, the index is empty and ``load_skill`` returns a clear error. The agent
still functions; it just cannot load additional skill guidance.

Configuration env vars
----------------------
WORKFLOW_GITHUB_REPO   : ``owner/repo`` of the agent-workflow repo
                         (e.g. ``tiendv89/agent-workflow``). Required.
WORKFLOW_GITHUB_BRANCH : branch to read skills from (default: ``main``).
GITHUB_TOKEN           : personal access token with ``repo`` read scope.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_DEFAULT_TIMEOUT = 20

# Paths in the agent-workflow repo that contain loadable skills.
_TECHNICAL_SKILLS_PREFIX = "claude/technical_skills"
_AUTHORING_SKILLS = frozenset({"tech-lead", "init-feature"})
_WORKFLOW_SKILLS_PREFIX = "claude/workflow_skills"

# Skills in workflow_skills/ that are NOT loadable.
_MUTATION_SKILLS = frozenset({"approve-feature", "reject-feature", "set-feature-stage"})
_EXECUTION_SKILLS = frozenset({
    "start-implementation", "review-pr", "browser-qa-frontend",
    "sync-workspace-rules", "init-workspace", "pr-create",
    "respond-to-review", "generate-deployment-checklist",
    "list-features", "list-global-features",
    "run", "verify", "code-review", "simplify",
    "update-config", "keybindings-help", "statusline-setup",
    "fewer-permission-prompts", "loop", "init", "init-agent",
    "resolve-project-env", "gitnexus-mcp", "figma-mcp",
    "deep-research", "security-review", "pipeline-parity-qa",
    "rag-context", "rag-enforce", "claude-api",
    "set-feature-stage",
    "approve-feature", "reject-feature",
})

_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---", re.DOTALL)
_DESC_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)


@dataclass
class SkillEntry:
    """A single loadable skill in the index."""

    name: str
    description: str
    path: str
    is_authoring: bool = False
    references: Dict[str, str] = field(default_factory=dict)

    @property
    def body(self) -> str:
        """Return the cached full SKILL.md body (may be empty until loaded)."""
        return self._body

    @body.setter
    def body(self, value: str) -> None:
        self._body = value

    def __post_init__(self) -> None:
        self._body: str = ""


# ---------------------------------------------------------------------------
# Module-level index cache (loaded once at startup)
# ---------------------------------------------------------------------------

_index_lock = threading.Lock()
_index: Optional[Dict[str, SkillEntry]] = None
_index_built = False


def _gh_headers(token: str) -> dict:
    return {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }


def _decode_content(encoded: str) -> str:
    return base64.b64decode(encoded.replace("\n", "")).decode("utf-8")


def _parse_description(skill_md: str) -> str:
    m = _FRONTMATTER_RE.match(skill_md)
    if m:
        dm = _DESC_RE.search(m.group(1))
        if dm:
            return dm.group(1).strip()
    return ""


def _fetch_file(owner: str, repo: str, path: str, ref: str, token: str) -> Optional[str]:
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
    resp = requests.get(
        url,
        headers=_gh_headers(token),
        params={"ref": ref},
        timeout=_DEFAULT_TIMEOUT,
    )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("encoding") == "base64":
        return _decode_content(data["content"])
    return None


def _fetch_tree(owner: str, repo: str, ref: str, token: str) -> List[dict]:
    url = f"{_GITHUB_API}/repos/{owner}/{repo}/git/trees/{ref}"
    resp = requests.get(
        url,
        headers=_gh_headers(token),
        params={"recursive": "1"},
        timeout=_DEFAULT_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("truncated"):
        logger.warning("skills: GitHub tree response was truncated for %s/%s@%s", owner, repo, ref)
    return [item for item in data.get("tree", []) if item.get("type") == "blob"]


def _build_index_from_github(owner: str, repo: str, ref: str, token: str) -> Dict[str, SkillEntry]:
    """Fetch all loadable SKILL.md files and build the index."""
    try:
        blobs = _fetch_tree(owner, repo, ref, token)
    except Exception as exc:
        logger.warning("skills: failed to fetch git tree from %s/%s: %s", owner, repo, exc)
        return {}

    # Group blobs by skill path
    index: Dict[str, SkillEntry] = {}

    def _is_technical(path: str) -> bool:
        return path.startswith(_TECHNICAL_SKILLS_PREFIX + "/")

    def _is_loadable_authoring(path: str) -> bool:
        if not path.startswith(_WORKFLOW_SKILLS_PREFIX + "/"):
            return False
        parts = path[len(_WORKFLOW_SKILLS_PREFIX) + 1:].split("/")
        return parts[0] in _AUTHORING_SKILLS

    skill_md_paths = [
        p["path"] for p in blobs
        if p["path"].endswith("/SKILL.md") and (_is_technical(p["path"]) or _is_loadable_authoring(p["path"]))
    ]

    for skill_md_path in skill_md_paths:
        parts = skill_md_path.rsplit("/", 1)  # ["claude/technical_skills/foo", "SKILL.md"]
        skill_dir = parts[0]
        skill_name = skill_dir.rsplit("/", 1)[-1]

        content = _fetch_file(owner, repo, skill_md_path, ref, token)
        if content is None:
            logger.debug("skills: could not fetch %s — skipping", skill_md_path)
            continue

        description = _parse_description(content)
        if not description:
            logger.debug("skills: no description in frontmatter for %s — skipping", skill_name)
            continue

        is_authoring = _is_loadable_authoring(skill_md_path)

        entry = SkillEntry(
            name=skill_name,
            description=description,
            path=skill_dir,
            is_authoring=is_authoring,
        )
        entry.body = content

        # Collect reference files (all non-SKILL.md blobs under the same dir)
        for blob in blobs:
            bpath = blob["path"]
            if (
                bpath.startswith(skill_dir + "/")
                and not bpath.endswith("/SKILL.md")
                and "/" not in bpath[len(skill_dir) + 1:]  # direct children only (no nested subdirs)
            ):
                ref_name = bpath[len(skill_dir) + 1:]
                ref_content = _fetch_file(owner, repo, bpath, ref, token)
                if ref_content is not None:
                    entry.references[ref_name] = ref_content

        # Also include files in a references/ subdir
        for blob in blobs:
            bpath = blob["path"]
            ref_prefix = skill_dir + "/references/"
            if bpath.startswith(ref_prefix):
                ref_name = "references/" + bpath[len(ref_prefix):]
                if ref_name not in entry.references:
                    ref_content = _fetch_file(owner, repo, bpath, ref, token)
                    if ref_content is not None:
                        entry.references[ref_name] = ref_content

        index[skill_name] = entry
        logger.debug("skills: indexed %s (authoring=%s, refs=%d)", skill_name, is_authoring, len(entry.references))

    logger.info("skills: index built — %d skills (%d knowledge, %d authoring)",
                len(index),
                sum(1 for e in index.values() if not e.is_authoring),
                sum(1 for e in index.values() if e.is_authoring))
    return index


def build_index() -> Dict[str, SkillEntry]:
    """Build and cache the skills index from the agent-workflow GitHub repo.

    Thread-safe; fetches once and caches for the lifetime of the process.
    Returns an empty dict if configuration is missing.
    """
    global _index, _index_built

    with _index_lock:
        if _index_built:
            return _index or {}

        _index_built = True

        token = os.environ.get("GITHUB_TOKEN", "").strip()
        workflow_repo = os.environ.get("WORKFLOW_GITHUB_REPO", "").strip()
        ref = os.environ.get("WORKFLOW_GITHUB_BRANCH", "main").strip()

        if not token or not workflow_repo:
            logger.info(
                "skills: GITHUB_TOKEN or WORKFLOW_GITHUB_REPO not set — "
                "skill index will be empty (graceful degradation)"
            )
            _index = {}
            return {}

        if "/" not in workflow_repo:
            logger.warning("skills: WORKFLOW_GITHUB_REPO must be 'owner/repo', got %r — skipping", workflow_repo)
            _index = {}
            return {}

        owner, repo = workflow_repo.split("/", 1)
        logger.info("skills: building index from %s/%s@%s", owner, repo, ref)
        _index = _build_index_from_github(owner, repo, ref, token)
        return _index


def get_index() -> Dict[str, SkillEntry]:
    """Return the cached skill index, building it on first call."""
    global _index, _index_built
    with _index_lock:
        if _index_built:
            return _index or {}
    return build_index()


def get_skill(name: str) -> Optional[SkillEntry]:
    """Return a SkillEntry by name, or None if not found."""
    return get_index().get(name)
