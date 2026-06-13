---
name: data-engineer
description: Data engineering role guidance for pipelines, contracts, and backfill thinking.
---

## Rules
- Keep orchestration separate from data logic.
- Document inputs, outputs, and consumers.
- Consider idempotency and backfill impact.

## External label matching

CMS-sourced category labels (RSS `<category>`, feed tags, classification strings) can silently rename or recase without external notice. Never match them with exact string equality.

```python
# Bad — breaks silently on "press releases" or " Press Releases "
if "Press Releases" not in item.categories:
    ...

# Good — survives recasing and extra whitespace
target = "press releases"
if not any(target == cat.strip().lower() for cat in item.categories):
    ...
```

When a filter like this gates the pipeline's only real-time path, also add a fire-rate monitor: warn if the filter matches 0 items across a full feed pass (indicating a possible label change, not just a quiet day):

```python
if total_feed_items > 0 and matched == 0:
    logger.warning("Category filter matched 0 items — possible label rename")
```

## Skip counter granularity

Distinguish *filtered* skips (business-rule exclusion) from *dedup* skips (already known). Collapsing them into one counter hides filter outages behind normal dedup noise.

```python
# Bad — one counter for both
rss_skipped += 1

# Good — split so a filter outage is visible
if progress.kind == "filtered":
    rss_filtered += 1
else:
    rss_already_known += 1
```

## Log levels for pipeline drops

Use log levels consistently so operators can filter to meaningful signal:

| Event | Level | Reason |
|---|---|---|
| Article dropped (empty body, missing row) | `WARNING` | Unexpected data loss — should be investigated |
| Dedup skip (already known) | `INFO` | Expected, high-volume, not actionable |
| Fetch failure | `WARNING` | Transient error, worth watching |
| Parse exception | `ERROR` | Unexpected code path |

A systematic drop (e.g. trafilatura stops extracting bodies) must rise above INFO noise to be detectable. Individual drops log at WARNING; do not raise or set thresholds that fail the DAG.
