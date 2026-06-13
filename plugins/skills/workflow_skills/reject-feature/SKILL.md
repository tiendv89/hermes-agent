---
name: reject-feature
description: Reject a workflow stage while keeping lifecycle state consistent.
---

## Environment

### Required (implicit — via `workspace.yaml` actor_source)
| Variable | Description |
|---|---|
| `GIT_AUTHOR_EMAIL` | Written as the rejection actor; workspace.yaml uses `actor_source: env:GIT_AUTHOR_EMAIL` |

---

## Must
- record actor
- record timestamp
- record comment
- append history
- update `next_action`
- keep the feature in the correct lifecycle state for the rejected stage
