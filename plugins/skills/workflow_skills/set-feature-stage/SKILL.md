---
name: set-feature-stage
description: Move a feature intentionally back to or forward to a given workflow stage.
---

## Must
- preserve artifacts
- append history
- set revalidation flags
- update `current_stage`
- update `feature_status`
- update `next_action`
- never silently delete work

## Allowed target stages
- `product_spec`
- `technical_design`
- `tasks`
- `handoff`
