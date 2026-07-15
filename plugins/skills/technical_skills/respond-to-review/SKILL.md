---
name: respond-to-review
description: Address PR review comments (review-fixes mode) or rebase the task branch after a base-branch advance (conflict-resolution mode).
---

## GitNexus code lookup

If `query_gitnexus` is in your tool list, use it for structural lookups (symbol definitions, callers, impact analysis) before falling back to grep or file reads — it is one tool with a `tool=` selector; call `tool="list_repos"` first, then pass `repo="<name>"` on `query`/`context`/`impact`. If it is unavailable or returns no results, fall back to grep/Read — do not stop.

---

# Respond to Review

Two modes, dispatched by the `MODE` environment variable:

| `MODE` | When dispatched | What it does |
|---|---|---|
| `review-fixes` (default) | Task status `change_requested` — reviewer posted `REQUEST_CHANGES` | Fetch the specific review's inline comments via `$REVIEW_URL`, address each, resolve threads, set PR back to ready-for-review |
| `conflict-resolution` | Task status `in_review` and impl PR `mergeable: false` | Rebase the task branch onto its base, resolve conflicts safely (file ownership + dependency manifest union-merge), force-push |

Read `$MODE` first. If unset, default to `review-fixes`. Branch by mode below.

---

## Common context

Provided in your prompt under `## Your claimed task`:

| Key | Description |
|---|---|
| `Workspace root` | Absolute path to the management (workspace) repo (use `$WORKSPACE_ROOT` env var preferentially — executor's own clone on the task branch) |
| `Feature` | Feature ID |
| `Task ID` | Task ID |
| `Impl repo` | Implementation repo ID |
| `Repo root` | Absolute path to the impl repo (use `$TASK_REPO_PATH` env var preferentially) |
| `Branch` | Task branch (e.g. `feature/<feature_id>-<task_id>`) |
| `PR URL` | GitHub PR URL |

Env vars set by the executor:
- `$GITHUB_TOKEN` — required for all GitHub API calls
- `$RESULT_PATH` — write `result.json` here before exit (mandatory)
- `$MODE` — `review-fixes` or `conflict-resolution`
- `$REVIEW_URL` — set when `MODE=review-fixes`; the specific review event URL to address
- `$TASK_BASE_BRANCH` — set when `MODE=conflict-resolution`; the branch to rebase onto

## Before any changes (both modes)

Read the spec docs to stay in scope. If you've already read them in this session, skip:

1. `<Workspace root>/docs/features/<Feature>/product-spec.md`
2. `<Workspace root>/docs/features/<Feature>/technical-design.md`
3. The `## <Task ID>` section of `<Workspace root>/docs/features/<Feature>/tasks.md` — find bounds via `grep -n "^## T" <path>` then `Read` with `offset`+`limit`.

If a review comment or conflict resolution would require deviating from these, do **not** silently comply — note the conflict in the log entry / result.json notes.

---

# Mode A — review-fixes

Dispatched when the task is `change_requested` and the reviewer posted a `REQUEST_CHANGES` review.

## A.1 — Parse the review URL

`$REVIEW_URL` has the form `https://github.com/{owner}/{repo}/pull/{number}#pullrequestreview-{review_id}`.

If `$REVIEW_URL` is empty (legacy task with no review_url persisted), fall back to the broader "all unresolved threads" path documented in A.3.B. Otherwise extract `owner`, `repo`, `pull_number`, `review_id`.

## A.2 — Set PR to draft

Signals work in progress (best-effort):

```bash
curl -s -X PATCH \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}" \
  -d '{"draft": true}'
```

If it fails or the PR is already a draft, continue.

## A.3 — Fetch comments to address

### A.3.A — Precise path (when `$REVIEW_URL` is set)

Fetch the inline comments attached to this specific review:

```bash
curl -s \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}/reviews/{review_id}/comments"
```

Each item in the response has:
- `id` — comment database ID
- `path` — file path
- `line` (or `original_line`) — line number in the diff
- `body` — the actual change request
- `pull_request_review_id` — should match `{review_id}`

If the response is empty, the reviewer requested changes without inline comments (CI-only failure or holistic feedback). Skip to A.5 (run CI check + read the review body).

For each comment, also fetch the enclosing thread ID so you can resolve it in A.7. Run the GraphQL query from A.3.B (which returns all unresolved threads on the PR with their nested `comments.nodes[].databaseId`), then **filter to only the threads whose `comments.nodes[]` contains a `databaseId` matching one of the comment IDs returned by A.3.A**. The thread `id` from that filter is what `resolveReviewThread` needs in A.7. Other unresolved threads on the PR (from earlier reviews) are not your concern in `review-fixes` mode — leave them alone.

### A.3.B — Fallback path (when `$REVIEW_URL` is empty)

```bash
curl -s -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Content-Type: application/json" \
  "https://api.github.com/graphql" \
  -d '{
    "query": "query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100){nodes{id isResolved comments(first:10){nodes{databaseId path line body author{login}}}}}}}}",
    "variables": {"owner": "{owner}", "name": "{repo}", "number": {pull_number}}
  }'
```

Take threads where `isResolved: false`. For each, the first comment is the actionable request.

## A.4 — Address each comment

Use `$TASK_REPO_PATH` (executor's isolated impl clone) — not `Repo root` from context.

```bash
cd "$TASK_REPO_PATH"
git fetch origin
git checkout -B "<Branch>" "origin/<Branch>"
```

For each comment from A.3:
1. Read the requested change.
2. Open the referenced file at the referenced line.
3. Apply the fix.
4. Stage + commit:
   ```bash
   git add <changed-files>
   git commit -m "fix: address review comment — <brief description>"
   ```

Batch closely related changes into one commit. Unrelated fixes get separate commits.

Run any available local lint / type-check (`npm run typecheck`, `tsc --noEmit`, `gofmt -l .`, `ruff check .`) before pushing. If the tool is not installed (exit 127), skip — CI will check.

## A.5 — If A.3 found nothing — check CI

The REQUEST_CHANGES may be a CI failure with no inline comments:

```bash
HEAD_SHA=$(curl -s -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}" | jq -r '.head.sha')

curl -s -H "Authorization: Bearer $GITHUB_TOKEN" \
  "https://api.github.com/repos/{owner}/{repo}/commits/$HEAD_SHA/check-runs"
```

If any check has `conclusion: failure`:
- Fetch its annotations: `GET /repos/{owner}/{repo}/check-runs/{check_run_id}/annotations`
- Fix the failing code (in scope of the task spec, no further)
- Commit + push

If all CI checks pass (or none exist) and no inline comments existed, this is a state-mismatch — log it and proceed to A.7 / A.8 anyway to convert PR back to ready.

## A.6 — Push

```bash
git push origin "<Branch>"
```

Stop and report (write blocked result.json) if push is rejected. Do not retry blindly.

## A.7 — Resolve threads on GitHub

For each thread you addressed, resolve it via GraphQL so the REQUEST_CHANGES is satisfied:

```bash
curl -s -X POST \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Content-Type: application/json" \
  "https://api.github.com/graphql" \
  -d '{"query":"mutation($threadId:ID!){resolveReviewThread(input:{threadId:$threadId}){thread{isResolved}}}","variables":{"threadId":"<THREAD_ID>"}}'
```

If `resolveReviewThread` returns an error for a thread, log it and continue with the others.

## A.8 — Convert PR back to ready-for-review

```bash
curl -s -X PATCH \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/{owner}/{repo}/pulls/{pull_number}" \
  -d '{"draft": false}'
```

## A.9 — Write result.json + exit

```json
{"terminal_status": "in_review", "pr_url": "<PR URL>"}
```

If you could not push (A.6), write blocked instead:
```json
{"terminal_status": "blocked", "blocked_reason": "push_rejected", "blocked_suggestion": "<details>", "pr_url": "<PR URL>"}
```

Do not modify the task YAML — the orchestrator handles state transitions.

---

# Mode B — conflict-resolution

Dispatched when the task is `in_review` and `checkInReviewPrs` reports `mergeable: false`. Your job: rebase the task branch onto `$TASK_BASE_BRANCH` and resolve any conflicts within the agent's authored scope.

## B.1 — Set up

```bash
cd "$TASK_REPO_PATH"
git fetch origin
git checkout -B "<Branch>" "origin/<Branch>"
```

Capture the set of files the agent authored on this branch — needed for the file-ownership guard in B.3.c:

```bash
git diff --name-only "origin/$TASK_BASE_BRANCH...HEAD" > /tmp/agent-authored-files.txt
```

Abort any in-progress rebase before starting:

```bash
git rebase --abort 2>/dev/null || true
```

## B.2 — Attempt the rebase

```bash
git rebase "origin/$TASK_BASE_BRANCH"
```

Three outcomes — branch by exit status:

### B.2.a — Clean rebase (exit 0)

Skip to B.4 (push + result.json).

### B.2.b — Conflicted rebase

```bash
git diff --name-only --diff-filter=U > /tmp/conflicted-files.txt
```

Branch by who owns the conflicted files (B.3).

### B.2.c — Other error (corrupted index, etc.)

`git rebase --abort`, write blocked result.json with `blocked_reason: "rebase_error"`, exit.

## B.3 — Resolve conflicts (safely)

For every file in `/tmp/conflicted-files.txt`:

### B.3.a — Dependency manifests get a union merge

If the file basename is one of `package.json`, `pyproject.toml`, `requirements.txt`, `go.mod`, `Cargo.toml`:
- Take the union of both sides. Include all packages/versions from both `HEAD` and `$TASK_BASE_BRANCH`. Where two sides specify different versions for the same package, take the HIGHER version.
- For `package.json`: merge `dependencies` and `devDependencies` keys; do not drop entries from either side.

### B.3.b — Agent-authored conflicts get auto-resolved

If the file is in `/tmp/agent-authored-files.txt`:
- Read both sides of the conflict (between `<<<<<<<` and `>>>>>>>`).
- Preserve ALL functionality from both sides. Do not drop code unless it's literally identical.
- Use comments to mark intentional choices if unsure.

### B.3.c — Human-authored files are off-limits

If the conflicted file is **not** in `/tmp/agent-authored-files.txt`, **do not modify it**. This is a human-owned file. Auto-resolving here risks dropping work the human committed.

Instead:
1. `git rebase --abort`
2. Write blocked result.json:
   ```json
   {"terminal_status": "blocked", "blocked_reason": "pr_conflict_human_files", "blocked_suggestion": "Conflicts in human-owned files: <comma-separated paths>. Manual resolution required.", "pr_url": "<PR URL>"}
   ```
3. Exit.

### B.3.d — Continue the rebase

After resolving conflicts in agent-authored / dependency files:

```bash
git add -A
GIT_EDITOR=true GIT_SEQUENCE_EDITOR=true git rebase --continue
```

If `--continue` fails, repeat B.2.b/B.3 (a multi-commit rebase can stop at several commits in sequence). Cap at **10 rounds** to avoid infinite loops:
- If the same set of files is still conflicted after one resolution pass, your fix didn't work — go to B.3.c's blocked path.
- If 10 rounds elapse without clean completion, blocked path.

## B.4 — Push

```bash
git push --force-with-lease origin "<Branch>"
```

Force-with-lease is required because a rebase rewrites commit hashes. The `--force-with-lease` flag refuses the push if the remote has new commits we don't have — protects against clobbering concurrent pushes.

If push is rejected: `git rebase --abort`, write blocked result.json with `blocked_reason: "push_rejected_after_rebase"`, exit.

## B.5 — Write result.json + exit

On clean rebase + successful force-push:

```json
{"terminal_status": "in_review", "pr_url": "<PR URL>", "notes": "Rebased onto <TASK_BASE_BRANCH> and force-pushed; conflict_state resolved."}
```

The orchestrator reads this and writes `conflict_state: resolved` to the task YAML (you don't touch the YAML directly).

---

## Hard rules (both modes)

1. **Write result.json to `$RESULT_PATH` before exit.** Mandatory — a text-only response without it is a failure.
2. **Do not modify task state directly.** The orchestrator owns workflow state.
3. **Stay in scope.** Spec docs (product-spec, technical-design, tasks.md) are the source of truth. If a review comment contradicts the spec, log the conflict in result.json `notes` and do not silently comply.
4. **Code 127 (missing tool)** → write `{"terminal_status": "blocked", "blocked_reason": "missing_tool"}` and exit. Do not try to install system tools.
5. **No --force without `--force-with-lease`.** Especially in B.4 — guards against clobbering concurrent pushes.
