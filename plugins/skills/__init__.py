"""Skills subsystem for hermes-agent.

Implements the progressive-disclosure skills pattern (G10, §4.7):

- Skill index: built by walking the skills bundled with the gateway under
  ``plugins/skills/`` (``technical_skills/`` and ``shared.md``). The bundle
  ships in the image, so skills load with no network access or GitHub token —
  update them by re-copying the tree and rebuilding.

- load_skill tool (plugins/tools/skills.py): returns the full SKILL.md body
  plus any reference files for a named skill on demand.

- pre_llm_call injection (plugins/hooks.py): injects skill name+description
  lines so the agent knows what's available; for the technical-design stage
  the matching technical_skills are surfaced prominently.

Loaded buckets
--------------
* Knowledge skills  — all ``technical_skills/*/SKILL.md``  (is_authoring=False)
* Workflow skills   — no longer bundled; that workflow is implemented directly
  as Python tools (``plugins/tools/*.py``) instead of markdown instructions.

Every bundled skill is loadable via ``load_skill``. The tool returns
guidance text only, so skills whose actions are handled elsewhere (mutation
skills reimplemented as gateway tools; execution skills that assume a
bash/git checkout) are still useful to load as reference.
"""

from .index import SkillEntry, build_index, get_index, get_shared_rules, get_skill

__all__ = ["SkillEntry", "build_index", "get_index", "get_shared_rules", "get_skill"]
