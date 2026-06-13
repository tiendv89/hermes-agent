---
name: backend-engineer
description: Backend engineering role guidance with compatibility and versioning rules.
---

## Rules
- One task changes one repo only.
- API changes must preserve backward compatibility by default.
- Follow the workspace API versioning standard.
- Breaking changes require explicit approval.

## Distributed cron safety (horizontal scaling)

In-process cron schedulers (`node-cron`, `cron`, etc.) start inside **every** pod. In a multi-pod deployment this means the same job fires N times simultaneously — once per pod.

**Always guard in-process cron jobs with a distributed lock when the service may run as more than one instance.**

Preferred pattern — Redis `SET NX PX` with a scope-keyed TTL:

```ts
const lockKey = `<job-name>:<scope>`;          // scope = month, date, run-id, etc.
const acquired = await redis.set(lockKey, '1', 'NX', 'PX', ttlMs);
if (!acquired) {
  logger.info(`[Job] Skipped — lock held by another instance`);
  return;
}
// ... proceed with the job
```

Rules:
- **Key must be scope-keyed** — include the logical unit of work (e.g. the target month `2026-05-01`) so the key does not bleed across separate runs.
- **TTL must expire before the next firing** — set TTL shorter than the cron interval so the lock releases cleanly between firings.
- **Keep the DB idempotency guard** — the Redis lock prevents concurrent attempts in the same tick; the DB constraint/check prevents duplicate work across ticks (e.g. a monthly job that fires on days 28–31).
- **Do not use `SET EX` (overwrite) instead of `SET NX`** — only the first pod to call `NX` wins; subsequent callers skip. An unconditional SET defeats the pattern.

A plain DB-level idempotency check (SELECT-then-INSERT) is **not** sufficient under horizontal scaling: the SELECT and INSERT are not atomic, so all pods can pass the check simultaneously before any one of them commits. The DB constraint saves data integrity but causes spurious errors on every losing pod.
