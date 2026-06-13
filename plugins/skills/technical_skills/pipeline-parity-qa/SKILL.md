---
name: pipeline-parity-qa
description: Use this skill to verify output parity between a legacy data pipeline and a migrated replacement before making a launch decision.
---

# Pipeline Parity QA

## When to Use

- Before switching a production data pipeline from a legacy implementation to a migrated replacement
- When a qualitative processor (LLM tagging, summarization, embedding, etc.) has been rewritten and needs sign-off
- When the team needs a documented, defensible launch decision: rely, shadow-only, or defer

---

## Concepts

### Parity Dimensions

Every comparison must cover these five dimensions:

| Dimension | What to Check |
|-----------|---------------|
| **Freshness** | Are records being produced at the same latency/recency? Compare `created_at` / `updated_at` distributions for the same input window. |
| **Completeness** | Are all eligible input records processed? Count inputs vs outputs; find any silent drops. |
| **Schema** | Do output records have the expected structure? Validate field names, types, and nesting against the canonical model. |
| **Volume** | Over the same input set and time window, do output record counts roughly match? Flag >5% divergence as a gap. |
| **Required Fields** | Are all non-nullable / required fields consistently populated? Check for unexpected nulls, empty strings, or missing keys. |

### Gap Categories

Every identified gap must be classified:

| Category | Meaning |
|----------|---------|
| `acceptable` | Within expected variance (e.g., LLM non-determinism, minor formatting). No user-facing impact. |
| `fix-now` | Must be resolved before the new pipeline can be relied upon. Blocks `rely` decision. |
| `defer` | Known gap, low enough risk to accept at launch. Must be tracked as a follow-on ticket. |

### Launch Decisions

| Decision | Meaning |
|----------|---------|
| `rely` | New pipeline is primary. Legacy can be turned off. No `fix-now` gaps remain. |
| `shadow-only` | New pipeline runs in parallel but its output is not served. Monitoring continues. At least one `fix-now` gap remains or confidence is insufficient. |
| `defer` | New pipeline is not ready. Stay on legacy. `fix-now` gaps must be resolved first. |

---

## Workflow

### Phase 1: Define Scope

```
1. Identify the specific processor being compared (e.g., tweet tagging, news summarization).
2. Identify the legacy output location (DB table, Qdrant collection, file path).
3. Identify the new output location.
4. Select a sample window: use the last 7–14 days of data unless otherwise specified.
5. Document the sample scope: date range, record count, source types included.
```

### Phase 2: Run the Five-Dimension Check

For each dimension, produce a result:

**Freshness**
```
- Query both legacy and new output for the sample window.
- Compare the distribution of output timestamps vs input timestamps.
- Record: median lag, max lag, and any records older than the expected SLA.
```

**Completeness**
```
- Count eligible inputs (e.g., fetch_results rows in the window that the processor should handle).
- Count outputs produced by legacy vs new for those inputs.
- Record: input count, legacy output count, new output count, drop rate.
```

**Schema**
```
- Pull 20–50 sample records from legacy and new outputs.
- Compare field names, value types, and nesting depth.
- Flag any fields present in one but absent in the other.
- For LLM-generated fields (tags, categories, sentiments): verify valid enum values are used.
```

**Volume**
```
- For the same input set: count total output records (rows / Qdrant points) from each.
- Compute divergence: (new_count - legacy_count) / legacy_count × 100%.
- Flag if divergence > 5%.
```

**Required Fields**
```
- For each required field in the output model: compute null/missing rate.
- Compare null rates between legacy and new.
- Flag any field where new null rate > legacy null rate + 1%.
```

### Phase 3: Classify Gaps

```
For each gap found across the five dimensions:
1. Assign category: acceptable / fix-now / defer
2. Write one sentence explaining the classification rationale
3. If fix-now or defer: create a follow-on ticket reference
```

### Phase 4: Record Launch Decision

```
1. Summarise: total gaps by category
2. If zero fix-now gaps → decision eligible for `rely`
3. If one or more fix-now gaps → decision must be `shadow-only` or `defer`
4. Record decision, rationale, and sign-off (tech lead + product owner)
```

---

## Output Format

Produce a `parity-report.md` file in the feature's `docs/` folder:

```markdown
# Parity Report — [Processor Name]

**Date**: YYYY-MM-DD  
**Sample window**: YYYY-MM-DD to YYYY-MM-DD  
**Input record count**: N  
**Legacy location**: [table/collection]  
**New location**: [table/collection]  

---

## Freshness
- Legacy median lag: Xm
- New median lag: Ym
- Verdict: [acceptable / gap]

## Completeness
- Inputs: N
- Legacy outputs: N (drop rate: X%)
- New outputs: N (drop rate: X%)
- Verdict: [acceptable / gap]

## Schema
- Fields compared: [list]
- Mismatches: [list or "none"]
- Verdict: [acceptable / gap]

## Volume
- Legacy count: N
- New count: N
- Divergence: X%
- Verdict: [acceptable / gap]

## Required Fields
| Field | Legacy null rate | New null rate | Verdict |
|-------|-----------------|---------------|---------|
| ...   | ...             | ...           | ...     |

---

## Gap Register

| # | Dimension | Description | Category | Follow-on |
|---|-----------|-------------|----------|-----------|
| 1 | ...       | ...         | ...      | ...       |

---

## Launch Decision

**Decision**: rely / shadow-only / defer  
**Rationale**: [1–3 sentences]  
**fix-now gaps remaining**: N  
**Signed off by**: [tech lead], [product owner]  
**Date**: YYYY-MM-DD
```

---

## Notes

- **LLM non-determinism**: Do not compare exact LLM output values field-by-field. Compare structure, coverage, and enum validity only.
- **Qdrant parity**: For vector stores, compare point count, payload schema, and collection name — not vector values.
- **Idempotency baseline**: Check that the new processor's idempotency mechanism (content hash tracking) is working before running the comparison — otherwise completeness figures will be misleading.
- **Separate environments**: If possible, run the comparison on a staging/replica DB to avoid polluting production state.
