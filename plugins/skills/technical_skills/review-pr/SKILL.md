---
name: review-pr
description: >-
  Autonomous PR reviewer that evaluates implementation quality against the task
  spec and technical design, posts a GitHub APPROVE or REQUEST_CHANGES review,
  and writes a structured result.json for the orchestrator to route.
---

## GitNexus code lookup

If `mcp__gitnexus__*` tools are in your tool list, use them for structural lookups
(symbol definitions, callers, impact analysis) before falling back to grep or file
reads. If the MCP is unavailable or returns no results, fall back to grep/Read.

---

# Review PR

Evaluates a pull request against the task specification and technical design,
posts a GitHub review (APPROVE or REQUEST_CHANGES), and writes `result.json`
with the reviewer verdict for the orchestrator to route.

## Context

All required values are provided in the agent context under **## Your claimed task**:

| Key | Description |
|---|---|
| `Workspace root` | Absolute path to the management (workspace) repo |
| `Feature` | Feature ID (e.g. `autonomous-task-orchestrator`) |
| `Task ID` | Task ID (e.g. `T3`) |
| `Impl repo` | Implementation repo ID |
| `Repo root` | Absolute path to the implementation repo |
| `Branch` | Feature branch name (e.g. `feature/autonomous-task-orchestrator-T3`) |
| `PR URL` | GitHub PR URL (`https://github.com/{owner}/{repo}/pull/{number}`) |
| `Result path` | Absolute path to write `result.json` |
| `Max review cycles` | Maximum number of review cycles before escalating |
| `Review cycle count` | Number of `in_review` log entries already on this task |

The `GITHUB_TOKEN` environment variable is set for all GitHub API calls.

---

## Cycle limit check — run first

Before doing anything else, check whether the review cycle limit has been reached:

1. Read `MAX_REVIEW_CYCLES` from the environment (default: `3`).
2. Count `in_review` log entries in the task YAML at
   `<Workspace root>/docs/features/<Feature>/tasks/<Task ID>.yaml`.
3. If the count is already ≥ `MAX_REVIEW_CYCLES`:
   - Write `result.json` immediately with `terminal_status: "escalate"`:
     ```json
     {
       "terminal_status": "escalate",
       "verdict": "escalate",
       "confidence": 1.0,
       "notes": "Review cycle limit reached. Human review required."
     }
     ```
   - Stop. Do not post a GitHub review. Do not read the PR diff.

---

## Before reviewing

Read the following documents in order to understand the full context:

1. `<Workspace root>/docs/features/<Feature>/product-spec.md` — original requirements
2. `<Workspace root>/docs/features/<Feature>/technical-design.md` — architecture decisions
3. `<Workspace root>/docs/features/<Feature>/tasks.md` under `## <Task ID> — <title>` — specific scope, subtasks, and acceptance criteria

Every finding must be grounded in these documents. Do not request changes that
contradict the approved spec.

---

## Execution steps

### Step 1 — Parse the PR URL

Extract `owner`, `repo`, and `pull_number` from the `PR URL`:

```
https://github.com/{owner}/{repo}/pull/{pull_number}
```

### Step 2 — Fetch the PR diff

```bash
curl -s \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.v3.diff" \
  "https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}"
```

Read the full diff. Note every file changed and every line added or removed.

### Step 3 — Wait for CI to resolve

Poll CI check-runs until all checks reach a terminal state (success, failure,
cancelled, or skipped). Maximum wait: 10 minutes; poll every 30 seconds.

```bash
curl -s \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/{owner}/{repo}/commits/{head_sha}/check-runs"
```

If any check-run has `conclusion: failure` or `conclusion: cancelled`:
- CI failed. Record this as a 🔴 finding with severity `blocker`.
- You do not need to wait for remaining checks.

If all check-runs have `conclusion: success` (or there are no check-runs):
- CI passed. Continue to rubric evaluation.

If the 10-minute timeout expires before all checks resolve:
- Treat as CI failure. Record as a 🔴 finding: "CI timed out — checks still pending."

### Step 4 — Evaluate the PR against the rubric

Apply every criterion in
`<Skill dir>/references/review_criteria.md` to the diff.

For each finding, classify severity:
- 🔴 **Blocker** — correctness or security issue; blocks merge
- 🟡 **Important** — performance or design issue; should fix
- 🟢 **Nit / suggestion** — style or minor improvement; does not block

Record findings with:
- File path and line reference (from the diff)
- Criterion from the rubric that was violated
- Clear description of the problem
- Concrete suggestion for how to fix it

### Step 5 — Apply the decision table

| Condition | Decision | `terminal_status` |
|---|---|---|
| Cycle count ≥ `MAX_REVIEW_CYCLES` | Escalate immediately | `escalate` |
| CI failed | REQUEST_CHANGES | `change_requested` |
| Any 🔴 finding | REQUEST_CHANGES | `change_requested` |
| Any 🟡 finding | REQUEST_CHANGES | `change_requested` |
| Only 🟢 findings or no findings | APPROVE | `passed` |

### Step 6 — Post the GitHub review (two-call pattern)

The review post is split into two independent API calls. **Both calls must be attempted in order**, even if the first one returns an unexpected error.

#### Step 6a — Post comment (always execute)

Post the full review narrative as a regular issue comment. This endpoint is **not** subject to the GitHub self-review restriction.

```bash
curl -s -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/{owner}/{repo}/issues/{pull_number}/comments" \
  -d '{
    "body": "<full review narrative: verdict, all findings with severity markers, inline references>"
  }'
```

This call must always succeed. If it fails (non-422 error), treat as a fatal error: log the response and write an `escalate` result. Do not suppress errors from this step.

Capture the `html_url` from the response body — use it as `review_url` in result.json if Step 6b is skipped.

#### Step 6b — Post review event (attempt after 6a; skip on 422)

Attempt to post the formal review event. Include inline comments for findings here so reviewers can see them in the GitHub diff view.

For **APPROVE**:
```bash
curl -s -w "\n%{http_code}" -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/reviews" \
  -d '{
    "event": "APPROVE",
    "body": "<brief summary>",
    "comments": [<inline 🟢 nit comments if any>]
  }'
```

For **REQUEST_CHANGES**:
```bash
curl -s -w "\n%{http_code}" -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/reviews" \
  -d '{
    "event": "REQUEST_CHANGES",
    "body": "<brief summary of all findings>",
    "comments": [<inline comments for each 🔴/🟡 finding>]
  }'
```

Inline comment format:
```json
{
  "path": "<file path>",
  "line": <line number in the diff>,
  "body": "🔴 **Blocker** — <description>\n\n<concrete suggestion>"
}
```

**Self-review handling**: Read the HTTP status code from the response:
- **HTTP 201** — review posted. Capture the `url` from the response body; record it in `result.json` as `review_url`.
- **HTTP 422** — GitHub self-review restriction. Emit `reviewer_self_review_skipped` to stdout:
  ```
  reviewer_self_review_skipped task_id=<task_id> feature_id=<feature_id> pr_number=<pull_number>
  ```
  Set `review_url` to `null` and `self_review_skipped: true` in `result.json`. Do **not** fail the executor — the comment in step 6a is the authoritative narrative. Proceed to Step 7.
- **Any other error** — fatal. Log the response body to stderr and write an `escalate` result with the error details. Stop.

### Step 7 — Merge the PR (APPROVE path only)

If the decision is **REQUEST_CHANGES** or **escalate**, skip this step entirely.

Step 7 is **best-effort**: success is not required for the task to eventually be
marked `done`. The orchestrator's in_review PR poll watches the implementation
PR on GitHub; whenever it sees `merged: true` (whether merged by this step or by
a human later), `handleMergedPrs` writes `status: done` and runs the auto-ready
cascade.

**Check whether the task requires human merge.** Re-read the task YAML at
`<Workspace root>/docs/features/<Feature>/tasks/<Task ID>.yaml` and check the value
of `execution.requires_human_review`. If `true`, skip the merge call entirely — the
APPROVE review has already been posted; the human will merge the PR. Proceed to Step 8.

Otherwise, squash-merge the implementation PR via the GitHub REST API:

```bash
curl -s -w "\n%{http_code}" -X PUT \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  -H "Content-Type: application/json" \
  "https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/merge" \
  -d '{"merge_method": "squash"}'
```

Read the HTTP status code:
- **HTTP 200** — merged successfully. Continue to Step 8.
- **HTTP 405** — PR already merged, or merge not allowed (e.g. branch protection
  requires a human approval). Continue to Step 8 — if the PR is open, the human
  will merge and the poll will catch it.
- **HTTP 409** — merge conflict. The branch needs to be rebased before it can merge. Write
  `terminal_status: "change_requested"` to result.json immediately and stop:
  ```json
  {
    "terminal_status": "change_requested",
    "verdict": "change_requested",
    "confidence": 1.0,
    "notes": "Merge conflict (HTTP 409) — branch must be rebased onto the base branch before merging."
  }
  ```
  Do not post a REQUEST_CHANGES review event for this case — the conflict is a branch
  management issue, not a code quality issue. The fix agent will rebase and re-push.
- **Any other error** — log the response to stdout. Continue to Step 8 — do not
  escalate for merge failure alone.

### Step 8 — Compute confidence

Assign a confidence score (0.0–1.0) reflecting how certain you are in the verdict:

- `1.0` — clear CI failure or obvious correctness bug with no ambiguity
- `0.8–0.9` — strong finding with clear rubric match
- `0.6–0.7` — judgment call (design tradeoff, ambiguous spec wording)
- `< 0.6` — escalate: confidence too low for autonomous decision

If confidence < `CONFIDENCE_THRESHOLD` (default: `0.80`), override the decision to
`terminal_status: "escalate"` regardless of the rubric outcome.

### Step 9 — Write result.json

Write the result file to the path provided in the agent context (`Result path`):

**On APPROVE / passed (review event posted):**
```json
{
  "terminal_status": "passed",
  "verdict": "passed",
  "confidence": 0.92,
  "notes": "All subtasks implemented. CI passed. No 🔴/🟡 findings.",
  "review_url": "https://github.com/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}"
}
```

**On APPROVE / passed (self-review restriction — review event skipped):**
```json
{
  "terminal_status": "passed",
  "verdict": "passed",
  "confidence": 0.92,
  "notes": "All subtasks implemented. CI passed. No 🔴/🟡 findings.",
  "review_url": "https://github.com/{owner}/{repo}/issues/{pull_number}#issuecomment-{id}",
  "self_review_skipped": true
}
```

**On REQUEST_CHANGES / change_requested (review event posted):**
```json
{
  "terminal_status": "change_requested",
  "verdict": "change_requested",
  "confidence": 0.88,
  "notes": "🔴 Missing null check in processTask() (src/poll/reap-loop.ts:47). 🟡 N+1 git-fetch inside loop (main.ts:203).",
  "review_url": "https://github.com/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}"
}
```

**On REQUEST_CHANGES / change_requested (self-review restriction — review event skipped):**
```json
{
  "terminal_status": "change_requested",
  "verdict": "change_requested",
  "confidence": 0.88,
  "notes": "🔴 Missing null check in processTask() (src/poll/reap-loop.ts:47).",
  "review_url": "https://github.com/{owner}/{repo}/issues/{pull_number}#issuecomment-{id}",
  "self_review_skipped": true
}
```

**On escalation:**
```json
{
  "terminal_status": "escalate",
  "verdict": "escalate",
  "confidence": 0.55,
  "notes": "Confidence below threshold. Review cycle limit or ambiguous spec — human review required."
}
```

---

## result.json schema

```json
{
  "terminal_status":     "passed" | "change_requested" | "escalate",
  "verdict":             "passed" | "change_requested" | "escalate",
  "confidence":          0.0 to 1.0,
  "notes":               "<one-line summary of findings>",
  "review_url":          "<GitHub review URL from Step 6b when posted; Step 6a comment URL when Step 6b returned 422; omit on escalate>",
  "self_review_skipped": true | false  // true when GitHub returned 422 on the review event POST
}
```

`result.json` **must** be written as the final step in every code path, including
on error. If you cannot complete the review, write:
```json
{
  "terminal_status": "escalate",
  "verdict": "escalate",
  "confidence": 0.0,
  "notes": "<reason for failure>"
}
```

---

## Error handling

| Situation | Action |
|---|---|
| PR diff fetch fails | Write `escalate` result with reason; stop |
| CI check-run API fails | Treat as CI timed out (🔴 finding); continue to review |
| Step 6a comment POST fails (non-422) | Write `escalate` result with reason; stop |
| Step 6b review POST returns 422 (self-review) | Emit `reviewer_self_review_skipped` to stdout; set `self_review_skipped: true`; continue without `review_url` |
| Step 6b review POST fails (other error) | Fatal — write `escalate` result with error details; stop |
| Step 7 merge returns 405 | PR already merged or branch protection — skip, continue |
| Step 7 merge returns 409 | Merge conflict — write `change_requested` result and stop; fix agent will rebase |
| Step 7 merge fails (other) | Log response to stdout; skip; continue — do not escalate for merge failure |
| Task YAML unreadable | Write `escalate` result; stop |
| Any `127` command not found | Write `escalate` result immediately; stop |

---

## Hard stop rule — missing tools

If any shell command exits with code 127, **first diagnose which command was not found**:

- Read the stderr/stdout output to identify the missing command name.
- If the **top-level tool** (e.g. `npm`, `pnpm`, `node`, `yarn`, `go`, `python`) is the one not found — the system tool is missing. Stop immediately and write:
  ```json
  {"terminal_status": "escalate", "verdict": "escalate", "confidence": 0.0, "notes": "missing_tool: <tool> command not found"}
  ```
- If the top-level tool **ran successfully** but a sub-command or binary it invoked was not found (e.g. `sh: vitest: not found` inside `npm run test`) — this is a missing project dependency, not a missing system tool. Diagnose and recover:
  1. Check whether the project dependency directory exists (e.g. `node_modules` for JS, virtual env for Python).
  2. If absent, install it using the appropriate command detected from the project (lock-file detection: `pnpm-lock.yaml` → `pnpm install --frozen-lockfile`, `yarn.lock` → `yarn install --frozen-lockfile`, `package-lock.json` → `npm ci`, `requirements.txt` → `pip install -r requirements.txt`, etc.).
  3. Re-run the original command once. If it exits 127 again, stop and escalate.

Do not attempt to install missing **system** tools or work around a missing top-level tool.
