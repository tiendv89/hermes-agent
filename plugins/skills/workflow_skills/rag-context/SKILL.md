---
name: rag-context
description: Inject RAG context for ad-hoc agent-initiated queries (e.g. before reviewing a PR or researching a topic). For implementation tasks, the runtime pre-injects RAG and writes the audit record itself — agents do not call this skill.
---

## When to use this skill

| Context | Should the agent invoke `rag-context`? |
|---|---|
| `/start-implementation` — same query as the runtime's pre-flight (`<task_title> <feature_id>`) | **No.** The runtime already fetched this; the result is injected at the top of your prompt as `## RAG Context`. Re-querying the same thing wastes a turn. |
| `/start-implementation` — **new topic** that comes up mid-implementation (a specific library, dependency, error pattern, edge case not in the task title) | **Yes.** Query with the new topic. Use the RAG MCP — it's the fastest way to find prior decisions, similar implementations, or related patterns. |
| Standalone (`/rag-context`) | Yes. Ad-hoc research before a design session or PR review re-do. |
| Any other agent-initiated lookup | Yes. |

The runtime is the **only authoritative writer** of `rag_context_injected` and `rag_summary` on the `rag_pre_flight` log entry. Agents must not mutate that entry — the runtime audits it on exit. Mid-task queries you make from this skill append a **separate** log entry (e.g. `action: rag_lookup`) which is fine.

> **Tool availability note:** `.mcp.json` is gitignored in this workflow. The `mcp__rag-server__rag_query` tool is only present when the operator has configured the RAG MCP server locally. When unavailable, the runtime records `tool_unavailable: MCP_RAG_URL not set` in the pre-flight entry — the run still proceeds, and the agent should not attempt to "fix" the missing context by re-summarising the prompt.

---

## Protocol

### Step 1 — Construct the query

Use the most specific description of the current work. In order of preference:

- If reviewing a PR or diff: `"<task_title> <feature_id> implementation review"`
- If writing a technical design: `"<feature title> technical design prior decisions"`
- Otherwise: a short natural-language description of the topic

### Step 2 — Check tool availability

If `mcp__rag-server__rag_query` does not appear in your tool list, the MCP server is not connected. Do not try to substitute prose for the missing tool. Stop and report the absence.

### Step 3 — Call the tool

```
tool:         mcp__rag-server__rag_query
query:        <constructed query from Step 1>
workspace_id: <workspace.yaml -> workspace_id>
top_k:        5
```

### Step 4 — Format and use results

Build a `## Relevant project context` section:
- Each chunk: `### <source_type>: <source_path>\n<content>`
- Cap at ~500 tokens (≈375 words) — truncate at the nearest chunk boundary
- Read these chunks **before** reading the task spec or writing any output — they are the baseline

---

## `rag_summary` vocabulary (runtime audit contract)

When the runtime writes the `rag_pre_flight` entry, `rag_summary` is one of the patterns below. The runtime audit on exit insists on this vocabulary; free-form prose like "RAG context provided via system prompt" is rejected.

| Pattern | Meaning |
|---|---|
| `Retrieved <N> chunks: <topics>` | Success — at least one chunk returned |
| `tool_unavailable: <reason>` | MCP server not configured / `MCP_RAG_URL` unset |
| `tool_call_failed: <error>` | Network error, timeout, or non-2xx response |
| `query_returned_no_chunks` | RAG server returned an empty result set |

Agents should not write entries with this `action: rag_pre_flight` value. If the runtime needs more information from a mid-task re-query, append a separate log entry with a different action name (e.g. `rag_lookup`).
