---
name: resume-feature
description: Resume a feature by summarizing current stage, approvals, blocking state, ready tasks, and next actions.
---

## Pre-flight

Before reading any feature artifacts, pull the latest state from the management repo:

```bash
git checkout main && git pull origin main
```

This ensures the summary reflects the current committed state, not a stale local snapshot.

## Task
Read the feature artifacts and summarize:
- current status
- current stage
- approvals completed
- approvals missing
- dependency state
- tasks that can proceed now
- blocked tasks
- deployment readiness if relevant
- next required human decision
- next role that should act

## Wording rule
Do not say "assign" unless the workspace explicitly defines an assignment mechanism.

Use wording like:
- "Tasks T1 and T2 are ready. Owning teams may begin execution."
- "Task T4 is blocked by dependency T1."
- "No human decision is required at this stage."
