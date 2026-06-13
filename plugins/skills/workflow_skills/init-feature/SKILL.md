---
name: init-feature
description: Create a new feature folder using the canonical feature templates from the shared `workflow` root.
---

## Environment

### Required
| Variable | Description |
|---|---|
| `WORKSPACE_ROOT` | Path to the root containing `workflow/templates/`; used to locate feature templates |

---

## Step 0 — Determine the feature owner (`go` or `ts`) — ask, never assume

Before creating anything, ask the human which orchestrator will drive this feature:

- **`ts`** (legacy) — the TypeScript/git orchestrator. Live task state lives in git as `tasks/T<n>.yaml`. This is the current model.
- **`go`** — the Go/Postgres orchestrator. Live task state lives in the database; the management repo holds only narrative (`product-spec.md`, `technical-design.md`, `tasks.md`). See feature `workflow-db`.

This is a **required answer**. Do not guess and do not silently pick one — if the human does not specify, ask again and wait. Only proceed once you have an explicit `go` or `ts`.

> **Owner convention (read-time only):** when *interpreting an existing feature*, an **absent `owner` field means `ts`** — every existing feature has no `owner`, and only a `go` feature carries an explicit `owner: go`. This backward-compat default is for reading features that already exist; it is **never** a substitute for an explicit answer here at creation time.

## Task
Create from `<WORKSPACE_ROOT>/workflow/templates/feature/`:
- `product-spec.md`
- `technical-design.md`
- `status.yaml`
- `handoffs/`
- **`ts` feature only:** `tasks/` (per-task YAML state lives here)

### For a `go` feature
- Add a top-level `owner: go` field to `status.yaml`.
- Do **not** create `tasks/*.yaml`, and omit the `tasks/` directory (or leave it empty) — a go feature has no git task-state files; its task state is created in the database. `tasks.md` (narrative) is produced later by `tech-lead`.

### For a `ts` feature (human explicitly chose `ts`)
- Current behavior. Leave `owner` absent (absent ⇒ ts at read time). Create `tasks/` for per-task YAML state.

## Rules
- tasks are stored one YAML file per task — **`ts` features only**; `go` features keep task state in the DB, not git
- **never assume the owner at creation** — Step 0 must be answered with an explicit `go` or `ts`; if unanswered, ask again, do not default. (The `absent owner ⇒ ts` rule is a read-time convention for *existing* features only — see Step 0.)
- subtasks remain inside the task file as checklist/log entries
- subtasks do not have their own lifecycle status
- do not create `deployment-checklist.md` too early
- task YAML templates must not include a `role:` field — agents are full-stack
- when generating a `tasks.md` template, include a `### Required skills` stub (empty list) under each `## T<n>` section
