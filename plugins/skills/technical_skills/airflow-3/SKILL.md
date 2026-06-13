---
name: airflow-3
description: Airflow 3.x DAG development standards for data engineering teams.
---

## Principles
- DAG = orchestration only
- Business logic must live outside DAG files
- Tasks must be idempotent
- Clear data flow (inputs, transformations, outputs, consumers)
- Environment-aware via configuration

## Requirements
- Each DAG must define owner
- DAGs must be safe to retry
- Backfill impact must be considered
- Inputs and outputs must be explicit

## Dynamic task mapping and backfill batching

When using `.expand()`, the mapped set must stay under Airflow's `core.max_map_length` (default: **1024**). Exceeding it causes the run to fail at mapping time — before any work executes.

**The hazard:** a source with a large historical corpus (e.g. 1,542 sitemap entries on first run) will blow the limit. Small rolling-window sources never hit it — the backfill case is new every time.

**Pattern — drain via batched query:**
```python
# Constant near the top of the lifecycle module
BATCH_LIMIT = 500  # well under max_map_length; tune to cluster config

def find_unresolved_ids(session, limit=BATCH_LIMIT):
    return session.execute(
        text("""
            SELECT id FROM discoveries
            WHERE NOT EXISTS (SELECT 1 FROM resolutions WHERE ...)
            ORDER BY first_seen_at   -- oldest first; drains the backfill predictably
            LIMIT :limit
        """),
        {"limit": limit},
    ).fetchall()
```

Rules:
- Cap the unresolved query with `LIMIT` + `ORDER BY first_seen_at` so each run processes the oldest pending batch and subsequent runs drain the rest.
- Keep the fetch/parse concurrency caps (`max_active_tis_per_dagrun`) separate — they control parallelism, not map size.
- Document the cap constant with the reason (map-length safety) so future maintainers don't remove it.
