---
name: pr-create
description: Create PR from current task branch and update task PR metadata.
---

## Environment

### Required
| Variable | Source |
|---|---|
| `GITHUB_ACCOUNT` | `printenv GITHUB_ACCOUNT` → fall back to `.env` |
| `GIT_AUTHOR_NAME` | Briefing Identity section → `printenv GIT_AUTHOR_NAME` → `.env` |
| `GIT_AUTHOR_EMAIL` | Briefing Identity section → `printenv GIT_AUTHOR_EMAIL` → `.env` |

> **SSH**: not required. In agent runtime `GIT_SSH_COMMAND` is set by the executor — `git push` works without any SSH config. In interactive mode git uses the local SSH agent / `~/.ssh/config`.

---

## Must resolve environment first

Before any git push or PR operation, use values already resolved during the current session — **do not** invoke `/resolve-project-env`. That skill loads a large document for work that can be done in a single `printenv` call.

```bash
printenv GITHUB_ACCOUNT GIT_AUTHOR_EMAIL GIT_AUTHOR_NAME 2>/dev/null; true
```

For any value still missing after `printenv`, read `.env`:

```bash
cat <workspace_root>/.env 2>/dev/null; true
```

If a required value is absent after both checks, stop and ask the user. Do not guess.

## Git / SSH rule

SSH authentication is handled transparently — do not attempt to configure it:

- **Agent runtime**: `GIT_SSH_COMMAND` is set by the executor before Claude is spawned.
- **Interactive**: git uses the local SSH agent / `~/.ssh/config`.

## Base branch resolution (required — do this before creating the PR)

The PR base branch is resolved in priority order:

1. **`TASK_BASE_BRANCH` env var** — set by the orchestrator to the feature branch
   (e.g. `feature/{featureId}`). When present, use it directly — do not read
   `workspace.yaml`.

   ```bash
   printenv TASK_BASE_BRANCH
   ```

2. **`workspace.yaml` `base_branch`** — fall back only when `TASK_BASE_BRANCH` is
   absent (interactive / non-orchestrated runs).

   Steps:
   1. Read `workspace.yaml` from the project root.
   2. Find the entry in `repos[]` whose `id` matches the task's `repo` field.
   3. Use that entry's `base_branch` value as the `"base"` field in the PR payload.

   Example — task with `repo: cycle`:
   ```yaml
   # workspace.yaml
   repos:
     - id: cycle
       base_branch: matthew   # ← use this, not "main"
   ```

   If `base_branch` is missing for the repo, stop and surface the error — do not
   default to `main`.

**Never override `TASK_BASE_BRANCH` with the `workspace.yaml` value.** The
orchestrator sets it precisely so task PRs target the feature branch, not the
repo's root base branch.

## Repository (owner/repo) resolution (required — do this before creating the PR)

The `<owner>/<repo>` in the API URL is **not** something to guess or infer from
commit output. Resolve it from the impl checkout's `origin` remote:

```bash
origin_url=$(git remote get-url origin)
# Normalize either form to "owner/repo":
#   git@github.com:OWNER/REPO.git   ->  OWNER/REPO
#   https://github.com/OWNER/REPO(.git) -> OWNER/REPO
slug=$(printf '%s' "$origin_url" | sed -E 's#^git@github.com:##; s#^https://github.com/##; s#\.git$##')
owner=${slug%%/*}; repo=${slug##*/}
```

**Renamed repos.** If `git push` printed `remote: This repository moved. Please
use the new location: …NEW.git`, the repo was renamed on GitHub. The old name
still redirects, but for the PR API call use the **new canonical name** from that
notice (or run `git remote get-url origin` after updating the remote). Do not
invent a name and do not mix old/new. The orchestrator's `enforce-pr-url` check
tolerates a rename (it reconciles the PR's repo against the task's expected repo
via the GitHub API), so a PR opened against the new canonical name is accepted —
but only when the name genuinely resolves to the same repository.

Never derive `<owner>/<repo>` from the Go module path, the task title, or any
heuristic — `git remote get-url origin` is the single source of truth.

## GitHub API rule

Do NOT use the `gh` CLI to create pull requests. Use the GitHub REST API via `curl` instead. This avoids requiring `gh` to be installed on the host or in agent containers.

Use `GITHUB_TOKEN` from the project `.env` for authentication. If `GITHUB_TOKEN` is not set, fall back to reading `~/.config/gh/hosts.yml` for the `oauth_token` under `github.com`.

Example (with base branch resolved via the priority rules above):
```bash
curl -s -X POST \
  -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/repos/<owner>/<repo>/pulls \
  -d '{"title":"...","body":"...","head":"<branch>","base":"<resolved_base_branch>","draft":true}'
```

**Always open as draft.** The orchestrator promotes the PR to ready-for-review when the task enters `in_review`. Do not pass `"draft": false`.

## AGENT_RUNTIME guard

Before executing, check:

```bash
printenv AGENT_RUNTIME
```

- If `1` — you are inside the agent runtime. Only push the branch and create the GitHub PR. **Skip all management-repo mutations** (task YAML PR metadata update, log entry append, status → in_review). Then immediately write `result.json` to `$RESULT_PATH` yourself — do NOT return the PR URL as text and expect `/start-implementation` Step 5 to handle it. Writing result.json via a tool call is mandatory here; a text-only response will end the headless session before Step 5 can run. See **Agent-runtime result.json** section below.
- If empty or absent — follow the full flow below, including all management-repo mutations.

## Must

- push branch if needed
- create PR against the base branch resolved from `workspace.yaml` (see **Base branch resolution** section above) using the GitHub REST API (not `gh` CLI)
- avoid duplicate PR creation
- **When `AGENT_RUNTIME` is not `1`**: update task PR metadata, append task log entry, set status → in_review
- **When `AGENT_RUNTIME=1`**: skip all task YAML and management-repo mutations; write `result.json` directly (see below)

## Agent-runtime result.json

**Only when `AGENT_RUNTIME=1`.** After the PR is created (or found to already exist), write `result.json` to `$RESULT_PATH` using the Write tool. Do not output the PR URL as plain text — that ends the headless session.

```json
{"terminal_status": "in_review", "pr_url": "<PR URL>"}
```

On test failure (PR opened as draft after 3 failed attempts):

```json
{"terminal_status": "blocked", "blocked_reason": "tests_failed", "pr_url": "<PR URL>", "blocked_suggestion": "<what failed and what to try next>"}
```

Write the file, then stop. Do not output any further text.
