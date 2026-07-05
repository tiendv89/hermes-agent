---
name: review-pr
description: >-
  Chat-native PR reviewer that fetches context via github_pr_context, evaluates
  the diff against review_criteria.md, and posts a GitHub APPROVE or
  REQUEST_CHANGES review via github_pr_review. No result.json, no task-state
  writes — chat-invoked reviews stop after posting.
---

## Code-graph lookup

If `query_gitnexus` is in your tool list, use it for structural lookups
(symbol definitions, callers, impact analysis) before falling back to grep or
file reads. If it is unavailable or returns no results, fall back to grep/Read.

---

# Review PR

Evaluates a pull request against the task specification and technical design,
then posts a GitHub review (APPROVE or REQUEST_CHANGES) via `github_pr_review`.

**Chat-invoked reviews stop after posting the review. There is no result.json,
no merge step, and no task-state write — those belong to the orchestrator's
own `/review-pr` skill.**

---

## Context

All required values come from the user's request:

| Key | Description |
|---|---|
| `PR URL` | GitHub PR URL (`https://github.com/{owner}/{repo}/pull/{number}`) |
| `Workspace root` | Absolute path to the management (workspace) repo |
| `Feature` | Feature ID (e.g. `autonomous-task-orchestrator`) |
| `Task ID` | Task ID (e.g. `T3`) |

---

## Before reviewing

Load the rubric and read the spec documents:

1. Call `load_skill("review-pr")` — this loads `review_criteria.md` and this
   skill doc into context. (You are already executing this skill if you read
   this line; the `load_skill` call has already happened.)

2. Read the task specification in order:
   - `<Workspace root>/docs/features/<Feature>/product-spec.md`
   - `<Workspace root>/docs/features/<Feature>/technical-design.md`
   - The `## <Task ID>` section of `<Workspace root>/docs/features/<Feature>/tasks.md`

Every finding must be grounded in these documents. Do not request changes that
contradict the approved spec.

---

## Execution steps

### Step 1 — Fetch PR metadata

Call `github_pr_context` with `action="metadata"` and the PR URL.

```
github_pr_context(action="metadata", pr_url="<PR URL>")
```

Record: title, author, base branch, head SHA.

### Step 2 — Fetch the PR diff

Call `github_pr_context` with `action="diff"`.

```
github_pr_context(action="diff", pr_url="<PR URL>")
```

Read the full diff. Note every file changed and every line added or removed.

Optionally fetch the changed-file list for a structural overview:

```
github_pr_context(action="files", pr_url="<PR URL>")
```

### Step 3 — Wait for CI to resolve

Call `github_pr_context` with `action="checks"`. The tool polls CI check-runs
for up to `CHAT_REVIEW_CI_POLL_TIMEOUT_SECONDS` (default 60 s) and returns a
`status` field:

```
github_pr_context(action="checks", pr_url="<PR URL>")
```

Interpret the result:

- `status: "all_passed"` — CI passed. Continue to rubric evaluation.
- `status: "failed"` — one or more check-runs failed or were cancelled. Record
  as a 🔴 finding with severity `blocker`: "CI failed — see check-run details."
- `status: "pending"` — CI has not resolved within the poll window. Tell the
  user to retry once CI finishes; do not block or guess the outcome.
- `status: "no_checks"` — no check-runs found. Treat as passing; continue.

If `status` is `"pending"`, stop here and reply with a message asking the user
to re-invoke the review once CI has finished.

### Step 4 — Fetch additional context (optional)

When the diff or task spec raises questions, use additional `github_pr_context`
actions to gather more information before evaluating:

```
github_pr_context(action="comments", pr_url="<PR URL>")     # existing discussion
github_pr_context(action="reviews",  pr_url="<PR URL>")     # prior review history
github_pr_context(action="commits",  pr_url="<PR URL>")     # commit messages
github_pr_context(action="file_at_ref", owner="…", repo="…", path="…", ref="…")  # full file content
```

### Step 5 — Evaluate the PR against the rubric

Apply every criterion in `review_criteria.md` (loaded via `load_skill`) to the
diff.

For each finding, classify severity:
- 🔴 **Blocker** — correctness or security issue; blocks merge
- 🟡 **Important** — performance or design issue; should fix
- 🟢 **Nit / suggestion** — style or minor improvement; does not block

Record findings with:
- File path and line reference (from the diff)
- Criterion from the rubric that was violated
- Clear description of the problem
- Concrete suggestion for how to fix it

### Step 6 — Apply the decision table

| Condition | Decision |
|---|---|
| CI failed | REQUEST_CHANGES |
| Any 🔴 finding | REQUEST_CHANGES |
| Any 🟡 finding | REQUEST_CHANGES |
| Only 🟢 findings or no findings | APPROVE |

If your confidence in the verdict is below 0.80 (ambiguous spec wording,
genuinely unclear finding), tell the user you are uncertain and explain what
additional context would help. Do not post a review you are not confident in.

### Step 7 — Post the GitHub review

Call `github_pr_review` with the verdict, the full narrative, and any inline
comments:

**For APPROVE:**

```
github_pr_review(
  pr_url="<PR URL>",
  event="APPROVE",
  body="<full review narrative: verdict, all findings with severity markers, inline references>",
  comments=[
    {"path": "<file>", "line": <n>, "body": "🟢 **Nit** — <description>"}
  ]  // optional; omit if no nits
)
```

**For REQUEST_CHANGES:**

```
github_pr_review(
  pr_url="<PR URL>",
  event="REQUEST_CHANGES",
  body="<full review narrative>",
  comments=[
    {"path": "<file>", "line": <n>, "body": "🔴 **Blocker** — <description>\n\n<suggestion>"},
    {"path": "<file>", "line": <n>, "body": "🟡 **Warning** — <description>"}
  ]
)
```

The tool returns `{ok, review_url, self_review_skipped}`:

- `ok: true, self_review_skipped: false` — formal review posted. Share
  `review_url` in your reply.
- `ok: true, self_review_skipped: true` — GitHub's self-review restriction
  prevented the formal review event, but the full narrative was posted as an
  issue comment. The `review_url` is the comment URL. This is expected when
  the bot token owns the PR.
- `ok: false` — tool failed. Describe the error to the user and ask them to
  retry or check `GITHUB_TOKEN` configuration.

### Step 8 — Summarize in chat

Reply to the user with:
- The verdict (APPROVE / REQUEST_CHANGES)
- A brief summary of any 🔴/🟡 findings (or confirmation that none exist)
- A link to the posted review (`review_url`)
- If `self_review_skipped: true`, note that the review was posted as a comment
  rather than a formal review event due to GitHub's self-review restriction

**Chat-invoked reviews stop here. There is no merge step.**

---

## Error handling

| Situation | Action |
|---|---|
| `github_pr_context` returns `ok: false` | Report the error to the user; do not proceed with an incomplete diff |
| CI `status: "pending"` | Tell the user to retry once CI resolves; stop |
| CI `status: "failed"` | Record as 🔴 finding and continue to rubric evaluation |
| `github_pr_review` returns `ok: false` | Report the error; suggest checking `GITHUB_TOKEN` |
| `github_pr_review` returns `self_review_skipped: true` | Normal path — use `review_url` (comment URL) in the summary |
| Confidence < 0.80 | Tell the user what is unclear; do not post a low-confidence review |
