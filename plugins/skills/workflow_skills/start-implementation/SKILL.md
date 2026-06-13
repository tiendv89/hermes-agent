---
name: start-implementation
description: Implement a claimed task and open a PR.
---

You are an agent running headlessly in the agent runtime. The orchestrator has
already claimed the task on the management repo and set status to `in_progress`
on branch `feature/<feature_id>-<task_id>`. Your job is to do the work, push
commits, open the PR, and write `result.json` before exiting.

## Code-graph lookup (GitNexus MCP)

If `mcp__gitnexus__*` tools appear in your tool list, use them for structural
code questions **before** falling back to grep or full-file reads:

- `mcp__gitnexus__query` — locate a symbol, function, class, or pattern
- `mcp__gitnexus__context` — get callers, callees, type relationships
- `mcp__gitnexus__impact` — run before refactors or deletions to understand
  blast radius
- `mcp__gitnexus__detect_changes` — map a git diff to affected symbols
- `mcp__gitnexus__route_map` / `tool_map` — for service / agent topologies

Fall back to `grep` or `Read` only when GitNexus returns no results or the
question is about raw file content (config, comments) rather than code
structure. Never open an entire file just to find a symbol.

If `mcp__gitnexus__*` tools are absent, the indexer hasn't completed a cycle
yet — fall back to grep without trying to "wait" for the MCP.

## Environment

Set by the executor before this skill runs — use directly, do not re-resolve:

| Variable | Meaning |
|---|---|
| `$TASK_REPO_PATH` | Cloned impl repo, already checked out on the task branch |
| `$RESULT_PATH` | Path you MUST write `result.json` to before exit |
| `$GIT_AUTHOR_EMAIL`, `$GIT_AUTHOR_NAME` | Git identity for commits |
| `$GITHUB_TOKEN` | Used by `/pr-create` |

Task context (workspace root, feature id, task id, branch, repo) is provided in
your prompt under `## Your assigned task`.

## Step 1 — Read the spec

Read these in order before writing any code. Prefer RAG (`mcp__rag-server__rag_query`)
when available; only fall back to a full file read if RAG returns nothing.

1. `<workspace_root>/docs/features/<feature_id>/product-spec.md`
2. `<workspace_root>/docs/features/<feature_id>/technical-design.md`
3. The `## T<n>` section of `<workspace_root>/docs/features/<feature_id>/tasks.md` —
   find the section bounds via `grep -n "^## T" <path>` first, then `Read` with
   `offset`+`limit`. Do not `cat` the whole file.

A `## RAG Context` block may already be injected near the top of your prompt by
the runtime — read it first; do not re-query RAG for the same task title.

But **do** query RAG for new topics that come up while implementing — a
specific library API, an error pattern, a similar implementation in another
repo. Invoke `/rag-context` with the new query, or call
`mcp__rag-server__rag_query` directly. Mid-task RAG queries are encouraged,
not restricted — only re-querying the runtime's pre-flight query is wasted.

## Step 2 — Detect prior state

If `handover.md` exists at `$TASK_REPO_PATH/handover.md`, a previous run left
WIP. Read it first. Resume from where it stopped — do not redo finished work.

Otherwise: fresh implementation.

> PR review comments (change_requested tasks) are **not** handled by this
> skill. The orchestrator dispatches a separate fix-briefing executor that
> runs `/respond-to-review` for those.

## Step 3 — Implement

Make the changes in `$TASK_REPO_PATH`. Commit incrementally as logical units
and push each commit to the task branch. Use commit messages of the form
`feat(<feature_id>/<task_id>): <what>` or `fix(...)`, `chore(...)`, etc.

Run the project's formatter before each commit:
- `package.json` with a `format` or `lint:fix` script → run it
- Go → `gofmt -w .`
- Python → `ruff format .` or `black .`
- Anything else → skip silently

### Testing — mandatory, not optional

**Write tests for the code you write.** This is non-negotiable for any task
that adds or changes logic:

1. **Unit tests for new logic.** Every new function, method, or branch that
   contains conditional logic gets a unit test covering at minimum the happy
   path plus one edge case (null/empty input, boundary value, failure path).
2. **Test plan execution.** Read the `### Test plan` subsection of the `## T<n>`
   block in `tasks.md`. Cover every item:
   - For runnable items (compile, type-check, unit/integration tests, lint):
     execute and verify the actual output.
   - For inspection items (config verification, route registration, logic
     trace): read the relevant files and explicitly confirm each claim.
3. **Run the full test suite** before opening the PR. Detect the runner from
   the project (`npm test`, `pnpm test`, `go test ./...`, `pytest`, etc.).
4. If tests fail and you can fix them, do so and re-run. Stop after **3 failed
   attempts** — open the PR as draft anyway and report the failure in
   `result.json` with `terminal_status: blocked, blocked_reason: tests_failed`
   (see Step 5).

   **Why open the PR despite failing tests?** This is the agent-runtime
   carve-out to the `Test-before-PR` rule in CLAUDE.md (see "Agent-runtime
   exception"). In agent-runtime mode the PR is the durable handover — the
   next agent (fix or human) needs a branch to inherit. The orchestrator
   routes a `tests_failed` block to the next cycle. Do not silently exit
   without a PR after 3 failed attempts.

If `tasks.md` has no test plan and the task adds testable logic, write
appropriate tests anyway — do not punt with "no tests required."

## Step 4 — Open the PR

Invoke `/pr-create`. The skill rebases onto the PR base branch, pushes, opens
the PR as draft, and (in agent runtime) writes `result.json` itself.

If `/pr-create` succeeds, you are done — `result.json` has been written for you.

## Step 5 — Write result.json yourself (only if /pr-create did not)

If you reach this point without `/pr-create` having written `result.json`
(unrecoverable error, no commits to push, etc.), write it yourself to
`$RESULT_PATH` and exit:

```json
{"terminal_status": "in_review", "pr_url": "<PR URL>"}
```

On block:
```json
{"terminal_status": "blocked", "blocked_reason": "<short slug>", "blocked_suggestion": "<concrete next step>", "pr_url": "<URL if a PR was opened>"}
```

## Hard rules

1. **Writing `result.json` to `$RESULT_PATH` is mandatory.** A text-only response
   without writing this file is treated as a failure by the orchestrator. The
   executor will retry you once with a focused continuation prompt if you exit
   without it, but do not rely on that — write the file yourself.

2. **Do not modify task YAML or any management-repo file.** The orchestrator
   owns workflow state. Your only management-repo write is the `started` log
   entry below — and **only for `ts` features**; a `go` feature writes none (see Rule #3).

3. **Append a `started` log entry to the git task YAML — `ts` features only.**

   First determine the feature owner: read `owner` from
   `<workspace_root>/docs/features/<feature_id>/status.yaml`. **An absent `owner` field means `ts`.**

   - **`owner: go`** → **skip this entire step.** A go feature has no
     `tasks/<task_id>.yaml` in git; its task state — including the `started`
     entry — lives in the database and is written by the Go orchestrator. Do not
     read, write, or commit any task YAML; proceed straight to implementation.
   - **`ts` (absent owner)** → append the `started` log entry before doing any
     implementation work, using the `yaml` Node package programmatically — do NOT
     use the Edit tool (it misplaces entries when `workspace_pr` follows `log`):

   ```bash
   node -e "
   const fs = require('fs'), yaml = require('yaml');
   const { execSync } = require('child_process');
   const p = '<workspace_root>/docs/features/<feature_id>/tasks/<task_id>.yaml';
   const at = execSync('date +%Y-%m-%dT%H:%M:%S%z').toString().trim();
   const t = yaml.parse(fs.readFileSync(p, 'utf8'));
   t.log.push({ action: 'started', by: '$GIT_AUTHOR_EMAIL', at, note: 'executor work phase begun.' });
   t.execution.last_updated_by = '$GIT_AUTHOR_EMAIL';
   t.execution.last_updated_at = at;
   fs.writeFileSync(p, yaml.stringify(t), 'utf8');
   "
   git -C "<workspace_root>" add "docs/features/<feature_id>/tasks/<task_id>.yaml"
   git -C "<workspace_root>" commit -m "chore(<task_id>): append started log entry"
   git -C "<workspace_root>" push origin "feature/<feature_id>-<task_id>"
   ```

   Do not write `rag_context_injected` or `rag_summary` on agent log entries —
   the runtime is the authoritative writer for those.

4. **If a shell command exits with code 127** (top-level tool not found —
   `node`, `go`, `python`, `pnpm`, etc.), write
   `{"terminal_status": "blocked", "blocked_reason": "missing_tool"}` to
   `$RESULT_PATH` and exit. Do not attempt workarounds — this is a container
   image problem. A 127 from a sub-command (e.g. `vitest` not found inside
   `npm run test`) is a project-dependency issue — install dependencies and
   retry once.

5. **Checkpoint discipline.** If you sense you have done many back-and-forth
   turns and are not yet ready to finish, commit and push WIP, write a
   `handover.md` next to `$RESULT_PATH` describing what's done / what's left,
   write `{"terminal_status": "blocked", "blocked_reason": "handover_for_continuation", "pr_url": "<URL if any>", "handover_path": "<path>"}`,
   and exit cleanly. A graceful checkpoint is richer than a post-hoc recovery.
