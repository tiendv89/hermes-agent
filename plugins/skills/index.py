"""Skill index — local bundle loader.

Loadable skills are bundled with the gateway under ``plugins/skills/``
(``technical_skills/`` and ``shared.md``). The index is built once by walking
that directory at startup and cached for the lifetime of the process. No
network access or GitHub token is required.

Buckets
-------
* Knowledge skills  — every ``technical_skills/*/SKILL.md`` (is_authoring=False)

Every bundled skill is loadable via ``load_skill`` — the tool only
returns the skill's guidance text, so even skills whose *actions* are handled
elsewhere (mutation skills reimplemented as gateway tools; execution skills
that assume a bash/git checkout) are still useful as reference.

Graceful degradation: if the bundle directory is missing the index is empty
and ``load_skill`` returns a clear error. The agent still functions; it just
cannot load additional skill guidance.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

logger = logging.getLogger(__name__)

# Root of the bundled skills (this package directory holds technical_skills/
# and shared.md).
_BUNDLE_ROOT = Path(__file__).resolve().parent

# Logical path prefix used for SkillEntry.path (cosmetic label).
_TECHNICAL_SKILLS_PREFIX = "technical_skills"

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


def _parse_description(skill_md: str) -> str:
    m = _FRONTMATTER_RE.match(skill_md)
    if m:
        dm = _DESC_RE.search(m.group(1))
        if dm:
            return dm.group(1).strip()
    return ""


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _collect_references(skill_dir: Path) -> Dict[str, str]:
    """Gather a skill's reference files: direct (non-SKILL.md) children plus the
    files in a ``references/`` subdirectory. Nested directories like ``rules/``
    or ``scripts/`` are intentionally ignored — the gateway can't execute them."""
    refs: Dict[str, str] = {}

    for child in sorted(skill_dir.iterdir()):
        if child.is_file() and child.name != "SKILL.md":
            content = _read(child)
            if content is not None:
                refs[child.name] = content

    ref_dir = skill_dir / "references"
    if ref_dir.is_dir():
        for child in sorted(ref_dir.iterdir()):
            if child.is_file():
                key = f"references/{child.name}"
                if key not in refs:
                    content = _read(child)
                    if content is not None:
                        refs[key] = content

    return refs


def _index_skill(skill_dir: Path) -> Optional[SkillEntry]:
    """Build a SkillEntry from a skill directory, or None if it has no SKILL.md
    with a frontmatter description."""
    content = _read(skill_dir / "SKILL.md")
    if content is None:
        return None

    description = _parse_description(content)
    if not description:
        logger.debug("skills: no description in frontmatter for %s — skipping", skill_dir.name)
        return None

    entry = SkillEntry(
        name=skill_dir.name,
        description=description,
        path=f"{_TECHNICAL_SKILLS_PREFIX}/{skill_dir.name}",
    )
    entry.body = content
    entry.references = _collect_references(skill_dir)
    return entry


def _build_index_from_bundle(root: Path) -> Dict[str, SkillEntry]:
    """Walk the bundled skills tree and build the loadable-skill index."""
    index: Dict[str, SkillEntry] = {}

    technical_dir = root / "technical_skills"
    if technical_dir.is_dir():
        for skill_dir in sorted(technical_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            entry = _index_skill(skill_dir)
            if entry is not None:
                index[entry.name] = entry

    logger.info("skills: index built from bundle — %d skills", len(index))
    return index


def build_index() -> Dict[str, SkillEntry]:
    """Build and cache the skills index from the bundled claude/ tree.

    Thread-safe; walks the bundle once and caches it for the process lifetime.
    Returns an empty dict if the bundle directory is missing.
    """
    global _index, _index_built

    with _index_lock:
        if _index_built:
            return _index or {}

        _index_built = True

        if not _BUNDLE_ROOT.is_dir():
            logger.warning(
                "skills: bundle directory %s not found — skill index will be empty",
                _BUNDLE_ROOT,
            )
            _index = {}
            return {}

        logger.info("skills: building index from bundle at %s", _BUNDLE_ROOT)
        _index = _build_index_from_bundle(_BUNDLE_ROOT)
        return _index


def get_index() -> Dict[str, SkillEntry]:
    """Return the cached skill index, building it on first call."""
    with _index_lock:
        if _index_built:
            return _index or {}
    return build_index()


def get_skill(name: str) -> Optional[SkillEntry]:
    """Return a SkillEntry by name, or None if not found."""
    return get_index().get(name)


# ---------------------------------------------------------------------------
# Shared workflow rules (shared.md)
# ---------------------------------------------------------------------------

_shared_rules_lock = threading.Lock()
_shared_rules: Optional[str] = None
_shared_rules_loaded = False


def get_shared_rules() -> str:
    """Return the bundled shared workflow rules (``shared.md``).

    These are the company-wide rules the agent should always follow (feature
    lifecycle, stage-review statuses, task statuses, the end-to-end workflow).
    The gateway injects them into the agent's system prompt every turn. Read
    once and cached; returns an empty string if the file is missing.
    """
    global _shared_rules, _shared_rules_loaded

    with _shared_rules_lock:
        if _shared_rules_loaded:
            return _shared_rules or ""

        _shared_rules_loaded = True
        content = _read(_BUNDLE_ROOT / "shared.md")
        if content is None:
            logger.warning(
                "skills: shared rules file %s not found — system prompt will not "
                "include the shared workflow rules",
                _BUNDLE_ROOT / "shared.md",
            )
            _shared_rules = ""
        else:
            _shared_rules = content
        return _shared_rules
