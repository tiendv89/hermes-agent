---
name: approve-feature
description: Approve a workflow stage and move state forward deterministically.
---

## Environment

### Required (implicit — via `workspace.yaml` actor_source)
| Variable | Description |
|---|---|
| `GIT_AUTHOR_EMAIL` | Written as the approval actor; workspace.yaml uses `actor_source: env:GIT_AUTHOR_EMAIL` |

---

## Must
- update the correct stage review state
- record actor
- record timestamp
- append review history
- move workflow forward if appropriate

## Stage effects
- approving `product_spec` moves the feature toward `in_tdd`
- approving `technical_design` advances to task planning
- approving `tasks` moves feature to `ready_for_implementation`
- approving `handoff` may move feature to `done`

## Task activation on `tasks` approval

When approving the `tasks` stage, after updating `status.yaml`, activate eligible tasks:

1. Read every `tasks/T<n>.yaml` file in the feature directory.
2. For each task with `status: todo`:
   - If `depends_on` is empty (`[]`), set `status: ready`.
   - If every task ID listed in `depends_on` has `status: done`, set `status: ready`.
   - Otherwise leave `status: todo` (dependencies are not yet met).
3. Append a log entry to each task that was changed:
   - `action: ready`
   - `by`: resolved `GIT_AUTHOR_EMAIL`
   - `at`: real UTC timestamp via `date -u +%Y-%m-%dT%H:%M:%SZ`
   - `note`: `"Task activated on tasks-stage approval — dependencies met."`

This ensures agents can immediately pick up Wave 1 tasks without a separate manual step.
