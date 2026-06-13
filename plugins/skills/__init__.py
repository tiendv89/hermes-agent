"""Skills subsystem for hermes-agent.

Implements the progressive-disclosure skills pattern (G10, §4.7):

- Skill index: fetches descriptions from the agent-workflow GitHub repo at
  startup (OQ5 resolution: GitHub API fetch rather than bundled snapshot —
  skills stay current without image rebuilds; GITHUB_TOKEN is already required
  for document write operations).

- load_skill tool (plugins/tools/skills.py): returns the full SKILL.md body
  plus any reference files for a named skill on demand.

- pre_llm_call injection (plugins/hooks.py): injects skill name+description
  lines so the agent knows what's available; for the technical-design stage
  the matching technical_skills are surfaced prominently.

Loaded buckets
--------------
* Knowledge skills  — all ``claude/technical_skills/*/SKILL.md``
* Authoring skills  — ``claude/workflow_skills/tech-lead/SKILL.md`` and
                      ``claude/workflow_skills/init-feature/SKILL.md``

Excluded buckets (not indexed or loadable)
------------------------------------------
* Mutation skills   — approve-feature, reject-feature, set-feature-stage
                      (reimplemented as hermes tools in T3)
* Execution skills  — start-implementation, review-pr, browser-qa-frontend,
                      sync-workspace-rules, init-workspace, and others that
                      assume bash/git/local-checkout the gateway does not have
"""

from .index import SkillEntry, build_index, get_index, get_skill

__all__ = ["SkillEntry", "build_index", "get_index", "get_skill"]
