---
name: sync-workspace-rules
description: Synchronize the shared workflow rules from the canonical shared `workflow` root into a project `CLAUDE.md` and `HERMES.md`, and verify/repair per-skill shared symlinks.
---

## Environment

### Required
| Variable | Description |
|---|---|
| `WORKSPACE_ROOT` | Path to root containing `workflow/claude/CLAUDE.shared.md`, `workflow/hermes/HERMES.shared.md`, and `workflow/scripts/install.sh` |

---

## Task

Sync the shared workflow rules into `CLAUDE.md` and `HERMES.md`, and repair skill symlinks.

**CLAUDE.md and HERMES.md sync must use the Read and Edit tools only — never bash.** The shared section markers contain `<!--` which corrupts bash tool calls in Claude Code. There is no safe bash pattern for this; use file tools exclusively.

## Path resolution

This skill requires:
- workspace root path
- project root path

### Workspace root resolution
Resolution order:
1. Read `.env` in the project root and look for `WORKSPACE_ROOT`
2. If present, use it
3. If missing, ask the user for the local workspace root path explicitly

Do not guess the workspace root.

### Project root resolution
Resolution order:
1. Use explicit `project_root` argument if provided
2. Otherwise use the current directory
3. Validate that the selected project root contains:
   - `workspace.yaml`
   - `CLAUDE.md`

If project root cannot be validated, require the user to answer with the project folder path.

## Step A — CLAUDE.md sync (Read and Edit only)

### Step A.1 — Read both files

Use the Read tool (not bash) to read:
1. `<project_root>/CLAUDE.md`
2. `<WORKSPACE_ROOT>/claude/CLAUDE.shared.md`

### Step A.2 — Compare

Locate the shared section in `CLAUDE.md`: it is the block between the `BEGIN SHARED WORKFLOW RULES` and `END SHARED WORKFLOW RULES` marker lines (the markers are HTML comments but treat them as plain delimiters — do not search for them with bash).

Compare the content between those markers against the full content of `claude/CLAUDE.shared.md`.

### Step A.3 — Update if needed

If the content differs, use the Edit tool to replace everything between the two marker lines with the current content of `claude/CLAUDE.shared.md`. Preserve:
- all content above the opening marker line
- all content below the closing marker line

If already in sync, skip the edit.

## Step B — HERMES.md sync (Read and Edit only)

This step runs on every invocation, independently of Step A. A failure in Step B must not prevent Step A from completing, and vice versa.

### Step B.1 — Check for HERMES.shared.md

Check whether `<WORKSPACE_ROOT>/hermes/HERMES.shared.md` exists (use the Read tool — if it returns an error the file is absent).

If absent: emit a warning to the user — `"hermes/HERMES.shared.md not found at <WORKSPACE_ROOT>/hermes/HERMES.shared.md — skipping HERMES.md sync (hermes_shared_missing)"` — and skip the rest of Step B. Do not treat this as a fatal error; return normally after the warning.

### Step B.2 — Read HERMES.shared.md

Use the Read tool to read `<WORKSPACE_ROOT>/hermes/HERMES.shared.md`.

### Step B.3 — Create or update HERMES.md

**If `<project_root>/HERMES.md` does not exist:**

Use the Write tool to create it with this template (substituting the actual project name from `workspace.yaml`):

```
# <project name> — Hermes operating rules
<!-- BEGIN SHARED WORKFLOW RULES -->
<content of hermes/HERMES.shared.md>
<!-- END SHARED WORKFLOW RULES -->
```

Where `<project name>` is the value of the `project` field (or `name` field) from `workspace.yaml`. If neither field is present, use the basename of `<project_root>`.

**If `<project_root>/HERMES.md` exists:**

Use the Read tool to read it, then locate the shared section between the `BEGIN SHARED WORKFLOW RULES` and `END SHARED WORKFLOW RULES` marker lines. Use the Edit tool to replace everything between those two marker lines with the current content of `hermes/HERMES.shared.md`. Preserve:
- all content above the opening marker line
- all content below the closing marker line

If already in sync, skip the edit.

## Must preserve
- project-local context above the shared section in both CLAUDE.md and HERMES.md
- project-specific additional rules below the shared section in both files

## Symlink repair

Two actions — run them in order. Do not skip either even if things appear healthy.

### Action 1 — Run repair-skills.sh

Run this single command. It handles broken symlink removal, install.sh, and git untracking in one pass:

```bash
<WORKSPACE_ROOT>/scripts/repair-skills.sh <project_root>
```

Do not reconstruct these steps manually. The script is the canonical repair path.

### Action 2 — Write `.claude/skills/.gitignore`

Use the Write tool to create or overwrite `<project_root>/.claude/skills/.gitignore` with exactly:

```
# Shared skill symlinks are managed by workflow/scripts/install.sh — do not commit them.
# Symlinks are git blobs, caught by *. Real local skill directories are trees, preserved by !*/.
*
!*/
!.gitignore
```

This prevents all current and future symlinks from being committed. Write it unconditionally — do not check whether it already exists first.

## settings.json sync

After symlink repair, sync `<WORKSPACE_ROOT>/templates/claude-settings.json` into `<project_root>/.claude/settings.json` using a union-by-matcher+command merge.

Use bash + jq for this step (bash is safe here — no HTML comment corruption risk).

### Algorithm

1. If `<project_root>/.claude/settings.json` does not exist, treat it as `{}`.
2. For each hook event type in the template (e.g. `PreToolUse`, `PostToolUse`):
   - For each `{matcher, hooks[]}` entry in the template event array:
     - Find the entry in the project with the same `matcher` value.
     - **No match:** append the entire template entry to the project's event array.
     - **Match found:** for each hook object in the template entry's `hooks[]`, add it to the project entry's `hooks[]` only if no existing hook already has the same `command` string.
3. Write the merged JSON (2-space indent) back to `<project_root>/.claude/settings.json`.

This merge is additive only — never remove or overwrite project-specific hooks.

## model_policy verification

After syncing rules and symlinks, verify that `workspace.yaml` contains `model_policy`.

Check that `model_policy` has entries for all five phases: `implementation`, `self_review`, `pr_description`, `suggested_next_step`, `conflict_resolution`. Each phase must have `allowed` (non-empty array) and `default` (must be in the `allowed` list).

If `model_policy` is absent, warn the user that it is required.

## Rules
- do not mutate project-specific rules
- do not copy shared skills into the project
- do not replace the entire `workflow_skills` directory with one symlink
- do not create symlinks that point to files — every per-skill symlink must point to a directory
- use `repair-skills.sh` as the single repair command — do not reconstruct its steps manually
- after running `repair-skills.sh`, always write `.claude/skills/.gitignore` with the Write tool — do not skip this even if the file exists
- do not use bash to check or write `.gitignore` — use the Write tool only
- if `WORKSPACE_ROOT` is missing, require the user to answer with the local workspace root path
- if `project_root` cannot be validated, require the user to answer with the project folder path
- `model_policy` in `workspace.yaml` must define all five phases with valid `allowed` + `default`
- Step A (CLAUDE.md) and Step B (HERMES.md) are independent — if one fails or is skipped, the other must still run
- if `hermes/HERMES.shared.md` is absent, emit the `hermes_shared_missing` warning and skip Step B silently — do not abort the invocation
- use the Read and Edit tools (never bash) for all HERMES.md sync operations, for the same reason as CLAUDE.md
