# PR Review Criteria

Criteria are applied in priority order. A finding in an earlier category supersedes
later categories in severity — a correctness bug outranks a style issue regardless
of how many style issues are present.

---

## Severity markers

| Marker | Severity | Reviewer action |
|---|---|---|
| 🔴 | Blocker — correctness or security | `REQUEST_CHANGES` — must fix before merge |
| 🟡 | Important — performance or design | `REQUEST_CHANGES` — should fix |
| 🟢 / 💡 | Nit / suggestion | Inline comment only; still `APPROVE` if no 🔴/🟡 |

The PR is approved if and only if there are **zero 🔴 or 🟡 findings**.

---

## 1. Correctness

*Priority 1 — highest weight.*

Check that the implementation produces the correct output for all inputs and
handles edge cases without crashing or producing wrong results.

| Check | Blocker? |
|---|---|
| Logic errors — incorrect algorithm, wrong formula, inverted condition | 🔴 |
| Off-by-one errors in loops, slice indices, pagination | 🔴 |
| Null / undefined dereference — accessing a field on a potentially-null value without guard | 🔴 |
| Missing error handling for an operation that can fail (network, FS, parse) | 🔴 |
| Race condition — shared mutable state accessed from concurrent code paths without synchronisation | 🔴 |
| Incorrect async flow — `await` missing, promise swallowed, unhandled rejection | 🔴 |
| Wrong data type passed to a function or stored in a field | 🔴 |
| Subtask listed in `tasks.md` not implemented or partially implemented | 🔴 |
| Test plan item from `tasks.md` not covered by a test | 🟡 |
| Edge case handled in tests but not in production code | 🔴 |
| Hardcoded value that should be configurable per the spec | 🟡 |

---

## 2. Security

*Priority 2.*

Check that the implementation does not introduce vulnerabilities.

| Check | Blocker? |
|---|---|
| Command injection — unsanitised user input passed to `exec`/`spawn`/`eval` | 🔴 |
| Path traversal — unsanitised input used in file path construction | 🔴 |
| SQL injection — dynamic query construction from user input | 🔴 |
| XSS — user input rendered in HTML without escaping | 🔴 |
| Hardcoded secret, API key, password, or token in source code | 🔴 |
| Secret logged at any log level | 🔴 |
| Auth / authz gap — an endpoint or operation that should require authentication does not check it | 🔴 |
| Missing input validation at system boundary (HTTP request, CLI arg, file read) | 🟡 |
| Overly permissive CORS, file permissions, or IAM policy | 🟡 |
| Dependency with known CVE introduced without justification | 🟡 |
| Sensitive data returned in API response when not needed | 🟡 |

---

## 3. Performance

*Priority 3.*

Check that the implementation does not introduce unacceptable latency or resource
usage for the expected workload described in the technical design.

| Check | Blocker? |
|---|---|
| N+1 query — a query is issued inside a loop when a single batched query could replace it | 🟡 |
| Synchronous / blocking I/O on the main event loop when async is available | 🟡 |
| O(n²) or worse algorithm where the technical design implies n can be large (> 1 000) | 🟡 |
| Loading an entire large file or dataset into memory when streaming would suffice | 🟡 |
| Missing index on a frequently-queried column (database schema changes) | 🟡 |
| Polling interval or retry loop with no back-off, leading to thundering-herd risk | 🟡 |
| Unbounded in-memory cache or queue with no eviction policy | 🟢 |

---

## 4. Design and architecture

*Priority 4.*

Check that the implementation matches the architectural decisions in the
technical design and follows existing codebase patterns.

| Check | Blocker? |
|---|---|
| Single responsibility violated — a module or function handles multiple unrelated concerns | 🟡 |
| DRY violation — logic duplicated across files when a shared utility already exists | 🟡 |
| Abstraction mismatch — implementation bypasses a port/adapter or breaks a layer boundary | 🟡 |
| New dependency added that duplicates an existing library already in `package.json` / `go.mod` | 🟡 |
| Configuration value embedded in application code instead of read from env / config | 🟡 |
| Wrong layer for a concern (e.g. business logic in a view, DB query in a controller) | 🟡 |
| Side-effects in a function declared as pure / no-side-effects | 🟡 |
| Missing interface or type definition that the tech design says should exist | 🟡 |
| Public API wider than the tech design specifies (extra exported symbols without justification) | 🟢 |
| Commented-out code left in without explanation | 🟢 |

---

## 5. Style and conventions

*Priority 5 — lowest weight.*

Check that the implementation follows the project's style and lint rules. These
findings are nits unless a linter failure is confirmed.

| Check | Blocker? |
|---|---|
| Linter / formatter rule violation (confirmed by running the project's lint command) | 🟡 |
| Naming convention violated — variable, function, type, or file named inconsistently with surrounding code | 🟢 |
| Unnecessary comment describing *what* the code does rather than *why* | 🟢 |
| Missing or misleading JSDoc / godoc / docstring where the project convention requires one | 🟢 |
| Import order or grouping inconsistent with the project's `eslint-plugin-import` / `goimports` config | 🟢 |
| Dead code — exported symbol or file never referenced outside the PR | 🟢 |
| Test file not co-located with its module when the project uses a co-location convention | 🟢 |
| TODO comment without a linked issue or task ID | 🟢 |

---

## Evaluation checklist

Before posting the GitHub review, verify every item below:

- [ ] All subtasks from `tasks.md` are implemented (Correctness)
- [ ] All test-plan items from `tasks.md` have a corresponding test (Correctness)
- [ ] CI check-runs have all reached a terminal state (Correctness)
- [ ] No secrets or hardcoded credentials (Security)
- [ ] No obvious injection or path-traversal surface (Security)
- [ ] No N+1 or blocking-I/O patterns (Performance)
- [ ] Implementation matches the architecture in `technical-design.md` (Design)
- [ ] No DRY violations against the existing codebase (Design)
- [ ] Linter passes (Style) — run the project's lint command to confirm
- [ ] Confidence score ≥ threshold; if not, escalate (Cycle limit)
