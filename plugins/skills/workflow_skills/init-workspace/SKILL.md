---
name: init-workspace
description: Initialize a new workspace OR verify and repair an existing one. Detects mode from the current directory — runs guided setup if no workspace is found, runs health checks and fixes if one already exists.
---

## Task

Two modes — detect which applies before doing anything else.

---

## Mode detection

Check whether the current directory is an existing workspace:

1. Does `./workspace.yaml` exist?
2. Does `./CLAUDE.md` exist?

| Condition | Mode |
|---|---|
| Both exist | **Verify & repair** — check and fix the existing workspace |
| Neither or only one exists | **Create** — run the guided setup wizard |

Do not ask the user which mode to use. Detect it from the filesystem.

---

## Mode A — Verify & repair (existing workspace)

When the current directory is an existing workspace, verify its health and fix anything missing or broken. Work through each check in order. Report a summary at the end.

### Check 1 — `.env` file exists

- If `.env` is missing and `.env.template` exists: offer to create `.env` from the template. Do so if the user agrees.
- If both are missing: create `.env` from scratch using the template structure.

### Check 2 — Required variables populated

Read `.env` and check each required variable from the table in **Required vs. Deferrable**. Also read `workspace.yaml` to discover which `<REPO_ID_UPPER>_LOCAL_PATH` variables are expected (one per repo in the `repos:` list).

For each missing or empty required variable:
- Tell the user which variable is missing and which skill requires it.
- Run the relevant phase from the guided wizard (Phase 3 for git identity, Phase 4 for GitHub/SSH, etc.) to collect the value.
- Write the value into `.env` immediately — do not batch; fix as you go.

Do not prompt for deferrable variables unless the user asks.

### Check 3 — `.env.template` in sync

Compare `.env.template` against `.env`. For any variable key present in `.env.template` but missing or empty in `.env`, warn the operator and prompt them to add it (following the same flow as Check 2).

Do **not** modify `.env.template` automatically. The template is the source of truth — the sync direction is `.env.template` → `.env`, never the reverse. Keys that exist in `.env` but are absent from `.env.template` are not a problem and require no action.

### Check 4 — `workspace.yaml` integrity

Verify:
- `model_policy` section exists with per-phase `allowed` + `default` entries for: `implementation`, `self_review`, `pr_description`, `suggested_next_step`, `conflict_resolution`.
- At least one repo is declared under `repos:`.
- `management_repo: management-repo` field exists at the top level.
- `repos[0].id` is exactly `management-repo` — the management repo must be the first entry.
- A repo entry with `id: management-repo` exists and has `github`, `local_path`, and `base_branch` set.

For each issue found: report it and offer to fix it interactively.

### Check 5 — Skill symlinks

Execute all four repair steps in order. Do not skip any step even if things appear healthy.

**Step 1 — Run repair-skills.sh.** This single command handles broken symlink removal, install.sh, and git untracking:

```bash
<WORKSPACE_ROOT>/scripts/repair-skills.sh <current_directory>
```

Do not reconstruct these steps manually.

**Step 2 — Write `.claude/skills/.gitignore`.** Use the Write tool to create or overwrite the file unconditionally with exactly:

```
# Shared skill symlinks are managed by workflow/scripts/install.sh — do not commit them.
# Symlinks are git blobs, caught by *. Real local skill directories are trees, preserved by !*/.
*
!*/
!.gitignore
```

### Check 6 — `CLAUDE.md` shared section

Use the Read tool to read `CLAUDE.md`. Check whether it contains the `BEGIN SHARED WORKFLOW RULES` / `END SHARED WORKFLOW RULES` marker block and whether the content between them matches `CLAUDE.shared.md` (also read with the Read tool).

**Never use bash to check or compare this section.** The markers contain `<!--` which corrupts bash tool calls in Claude Code. Read both files with the Read tool and compare directly. If missing or stale, invoke `sync-workspace-rules`.

### Verify & repair summary

After all checks, print a status table:

```
Check                        Status
---                          ---
.env file                    ✓ exists / ✗ created
Required vars complete       ✓ all set / ✗ fixed: GIT_AUTHOR_EMAIL, SSH_KEY_PATH
.env.template in sync        ✓ no missing keys / ✗ warned: <list of keys missing from .env>
workspace.yaml integrity     ✓ / ✗ <description of issue>
Skill symlinks               ✓ / ✗ repaired (N broken removed, N untracked)
  └─ .claude/skills/.gitignore  ✓ / ✗ created or repaired
CLAUDE.md shared section     ✓ / ✗ resynced
```

If everything was already healthy, say so clearly. If repairs were made, list what changed.

---

## Mode B — Create (new workspace)

Guide the user step-by-step through creating a new workspace using `<WORKSPACE_ROOT>/templates/workspace/`.

Do NOT create any files until all **Required** values have been collected. Walk the user through each phase in order, asking one group of questions at a time. Confirm values before writing.

---

## How required variables are determined

Each workflow skill declares its own `## Environment` section listing the variables it needs. Before asking the user anything, read the `## Environment` section of every skill being installed (all files under `<WORKSPACE_ROOT>/workflow/workflow_skills/*/SKILL.md`) and aggregate a unified required/deferrable list.

The tables below reflect the current aggregated result. If new skills are added, re-aggregate before running the wizard.

---

## Required vs. Deferrable

### Required — must be answered before any files are written

Aggregated from all skill `## Environment` sections. These values are load-bearing.

| Variable / Decision | Declared by | Why it's required |
|---|---|---|
| `WORKSPACE_ROOT` | `resolve-project-env`, `init-feature`, `sync-workspace-rules` | All skill path resolution depends on this |
| `WORKSPACE_ID` | `init-workspace` | Unique slug (used in file paths and YAML keys) |
| `WORKSPACE_NAME` | `init-workspace` | Human-readable project name |
| `GIT_AUTHOR_NAME` | `resolve-project-env`, `pr-create`, `start-implementation` | Git identity for commits; used as approval actor |
| `GIT_AUTHOR_EMAIL` | `resolve-project-env`, `approve-feature`, `reject-feature` | Directly referenced as `actor_source: env:GIT_AUTHOR_EMAIL` in workspace.yaml |
| `GITHUB_ACCOUNT` | `resolve-project-env`, `pr-create` | Required for PR creation and SSH remote URLs |
| `SSH_KEY_PATH` | `resolve-project-env`, `pr-create`, `start-implementation` | Required for SSH-based repo access; do not assume `~/.ssh/id_rsa` |
| Management repo (`repos[0].id: management-repo`) | `start-implementation`, `init-workspace` | The management repo stores all task state; must be first in repos[] with the fixed id |
| `management_repo: management-repo` in workspace.yaml | `start-implementation` | Explicit pointer so workflow skills can locate task state without array-position logic |
| `MANAGEMENT_REPO_LOCAL_PATH` | `resolve-project-env`, `start-implementation` | Local path to the management repo clone; required for claim commits |
| `<REPO_ID_UPPER>_LOCAL_PATH` per repo | `resolve-project-env`, `start-implementation` | Required for any git task execution |
| `base_branch` per repo | `start-implementation`, `pr-create` | Per-repo base branch declared in `workspace.yaml`; no global default |
| `model_policy` per-phase model allowlist | `init-workspace` | workspace.yaml centralized model cost control — defines allowed models + default per phase |

### Deferrable — can be set later

These can be left blank or use defaults; the user can fill them in before first feature.

| Variable / Decision | Default | Notes |
|---|---|---|
| `SKIP_STAGING` | `false` | Change if project has no staging environment |
| Additional repo local paths | empty | Add as more repos are onboarded |
| Project purpose/description | placeholder | Fills the CLAUDE.md project context section |
| Non-standard environments | develop/staging/production | Remove or rename if the project differs |
| `infra_engineer` / `qa` actor type | `human` | Change to `agent` if automated |

---

## Mode B — Step-by-step guided flow (create)

Walk through each phase in order. Ask, receive, confirm, then move to the next phase. Never skip a required phase.

### Phase 1 — Workspace root

1. Read `.env` in the current directory and look for `WORKSPACE_ROOT`.
2. If found, confirm it with the user: _"I found WORKSPACE_ROOT=<value>. Is this correct?"_
3. If missing, ask: _"What is your local workspace root path? (The folder that contains `workflow/` and your project folders)"_
4. Do not proceed until `WORKSPACE_ROOT` is confirmed.

### Phase 2 — Project identity

Ask all at once:

- **Workspace ID** — a short lowercase slug (e.g. `my-project`). This becomes the folder name and YAML key.
- **Workspace name** — human-readable (e.g. `My Project`).
- **Project purpose** — one sentence describing what this project does. (Can say "I'll fill this in later.")

Derive `WORKSPACE_ID` from the slug. Reject IDs with spaces or uppercase.

### Phase 3 — Git identity

1. Try to read git config: run `git config user.name` and `git config user.email`.
2. Show the result: _"Your git config has: name=<X>, email=<Y>. Use these?"_
3. If user says no, or values are empty, ask for them explicitly.
4. Both `GIT_AUTHOR_NAME` and `GIT_AUTHOR_EMAIL` are required.

### Phase 4 — GitHub & SSH

Ask:

- **GitHub account** — the GitHub username or organization that owns the repos for this project. Example: `mycompany`.
- **SSH key path** — the path to the SSH private key to use for git operations. Suggest detecting existing keys: check if `~/.ssh/id_ed25519` or `~/.ssh/id_rsa` exist and offer them as options. The user must confirm or provide an explicit path. Do not assume.

Both are required.

### Phase 5 — Repository setup

Every workspace requires a management repo as its first and mandatory repo entry. The management repo stores all task YAMLs, feature docs, and `CLAUDE.md` — it is the authoritative record of task state.

**Step 5a — Management repo (always first, id is fixed)**

The first repo is always the management repo. Its `id` is always `management-repo` — do not ask the user to choose an ID. Ask only:

1. **GitHub URL** — SSH URL of the management repo, e.g. `git@github.com:mycompany/my-project.git`
2. **Local path value** — the filesystem path to the local clone (can be left blank for now)
3. **Base branch** — required, no default. Ask explicitly; do not assume `main`.

Auto-derive the env var name as `MANAGEMENT_REPO_LOCAL_PATH`.

After collecting, set `management_repo: management-repo` in `workspace.yaml` and write the entry as `repos[0]`.

**Validation**: reject any workspace.yaml where `repos[0].id` is not `management-repo`. If a user tries to set a different ID for the first repo, explain the convention and correct it.

**Step 5b — Implementation repos**

Ask:

_"How many implementation repositories does this project have? (You can add more later.)"_

For each additional repo:

1. **Repo ID** — short slug, e.g. `my-project-api`
2. **GitHub URL** — full SSH URL, e.g. `git@github.com:mycompany/my-project-api.git`
3. **Local path env var name** — auto-suggest based on repo ID in uppercase, e.g. `MY_PROJECT_API_LOCAL_PATH`
4. **Local path value** — the actual local filesystem path to the repo clone (can be left blank for now)
5. **Base branch** — required, no default. Ask explicitly.
6. **Owner role** — which role owns this repo: `backend_engineer`, `frontend_engineer`, or `data_engineer`

Collect all repos before moving to the next phase.

### Phase 6 — Model policy

Explain to the user: _"The workspace defines which LLM models agents may use for each phase of task execution. This is the centralized cost-control surface."_

Show the four phases and ask the user to confirm or customize:
- **implementation** — the main code-writing step (default: `claude-sonnet-4-6`)
- **self_review** — agent reviews its own diff (default: `claude-sonnet-4-6`)
- **pr_description** — summarize changes for the PR (default: `claude-haiku-4-5-20251001`)
- **suggested_next_step** — brief explanation when blocked (default: `claude-haiku-4-5-20251001`)

For each phase, ask which models to allow and which to set as default. Accept the defaults if the user presses enter.

This populates `model_policy` in `workspace.yaml`.

### Phase 7 — Confirm and write

Show the user a summary of all collected values:

```
WORKSPACE_ROOT:      <value>
WORKSPACE_ID:        <value>
WORKSPACE_NAME:      <value>
GIT_AUTHOR_NAME:     <value>
GIT_AUTHOR_EMAIL:    <value>
GITHUB_ACCOUNT:      <value>
SSH_KEY_PATH:        <value>
Repos:               <list with id, github, local_path, base_branch, owner_role>
model_policy:        <per-phase allowed + default>
```

Ask: _"Everything look right? Type 'yes' to create the workspace, or let me know what to change."_

Do not write any files until the user confirms.

---

## Mode B — Must create (after confirmation)

- `<WORKSPACE_ROOT>/<WORKSPACE_ID>/CLAUDE.md`
- `<WORKSPACE_ROOT>/<WORKSPACE_ID>/workspace.yaml`
- `<WORKSPACE_ROOT>/<WORKSPACE_ID>/.env.template`
- `<WORKSPACE_ROOT>/<WORKSPACE_ID>/.env` (pre-filled with all confirmed values)
- `<WORKSPACE_ROOT>/<WORKSPACE_ID>/docs/overview.md`
- `<WORKSPACE_ROOT>/<WORKSPACE_ID>/docs/features/README.md`
- `<WORKSPACE_ROOT>/<WORKSPACE_ID>/docs/features/.gitkeep`
- `<WORKSPACE_ROOT>/<WORKSPACE_ID>/.claude/skills/.gitignore` (see content below)

## Must use

- `<WORKSPACE_ROOT>/workflow/CLAUDE.shared.md` — embed as the shared workflow section in `CLAUDE.md`
- Project-local context section (name + purpose) in `CLAUDE.md`
- Project-specific additional rules section in `CLAUDE.md`

## `.claude/skills/.gitignore` content

```
# Shared skill symlinks are managed by workflow/scripts/install.sh — do not commit them.
# Symlinks are git blobs, caught by *. Real local skill directories are trees, preserved by !*/.
*
!*/
!.gitignore
```

This ensures shared symlinks are never committed while real workspace-specific skill directories remain tracked. New symlinks added by install.sh are automatically ignored — no manual updates required. Broken symlinks (target missing) are also ignored, but should be cleaned up before install.sh runs.

## Must also invoke

```bash
<WORKSPACE_ROOT>/workflow/scripts/install.sh <WORKSPACE_ROOT>/<WORKSPACE_ID>
```

---

## Skill installation model

- `<project>/.claude/skills/` must remain a real directory
- shared skills must be symlinked one by one inside it
- project-specific local skills must remain possible

---

## workspace.yaml — repos section

The management repo is always first with the fixed id `management-repo`. Implementation repos follow.

```yaml
management_repo: management-repo

repos:
  # Management repo — always first, id is always management-repo
  - id: management-repo
    github: <github-url>
    local_path: env:MANAGEMENT_REPO_LOCAL_PATH
    base_branch: <base-branch>   # required; no default

  # Implementation repos
  - id: <repo-id>
    github: <github-url>
    local_path: env:<LOCAL_PATH_ENV_VAR>
    base_branch: <base-branch>   # required; no default
    owner_role: <role>
```

## workspace.yaml — model_policy

```yaml
model_policy:
  implementation:
    allowed: [claude-sonnet-4-6]
    default: claude-sonnet-4-6
  self_review:
    allowed: [claude-sonnet-4-6]
    default: claude-sonnet-4-6
  pr_description:
    allowed: [claude-haiku-4-5-20251001]
    default: claude-haiku-4-5-20251001
  suggested_next_step:
    allowed: [claude-haiku-4-5-20251001]
    default: claude-haiku-4-5-20251001
```

---

## .env.template content

The template should contain all variable names (no values), with comments grouping them:

```env
# Workspace identity
WORKSPACE_ROOT=
WORKSPACE_ID=<workspace-id>
WORKSPACE_NAME=<workspace-name>

# Git identity
GIT_AUTHOR_NAME=
GIT_AUTHOR_EMAIL=

# GitHub
GITHUB_ACCOUNT=
SSH_KEY_PATH=~/.ssh/your_key_name

# Workflow defaults
SKIP_STAGING=false

# Repo local paths
<REPO_ENV_VAR>=
```

The `.env` file should be pre-filled with all confirmed values.

---

## Rules

**Both modes:**
- Detect mode from the filesystem — do not ask the user
- Do not duplicate shared skills into the project
- Do not symlink the entire `workflow_skills/` directory as one link
- `model_policy` is required in `workspace.yaml` — it defines which models agents may use per phase
- If `WORKSPACE_ROOT` cannot be determined, ask the user before proceeding
- Use `repair-skills.sh` as the single repair command for symlinks — do not reconstruct its steps manually
- After running `repair-skills.sh`, always write `.claude/skills/.gitignore` with the Write tool — unconditionally, do not check first

**Create mode (Mode B) only:**
- Do NOT write any files until the user confirms the summary in Phase 7
- Do not leave the project in a half-installed state
- Remind the user at the end about any deferrable values left blank

**Verify & repair mode (Mode A) only:**
- Fix missing required vars as they are found — do not batch them to the end
- Write each fixed value to `.env` immediately after the user provides it
- Do not prompt for deferrable variables unless the user explicitly asks
- Do not overwrite values already set in `.env` — only fill in empty or missing ones
- Always run `install.sh` regardless of whether symlinks appear healthy
