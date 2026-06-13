---
name: gitnexus-mcp
description: Code-graph intelligence via GitNexus MCP. Use for structural code lookups — symbol definitions, call graphs, impact analysis, and cross-repo execution tracing — before falling back to grep or file reads.
---

## GitNexus MCP — code-graph tools

GitNexus exposes structural code intelligence tools under the `mcp__gitnexus__*` namespace. These tools operate on a pre-built AST + call-graph index and answer questions that grep and file reads cannot: "what calls this function", "what breaks if I delete this", "trace this execution path end-to-end."

### Tool reference

| Tool | MCP name | When to use |
|---|---|---|
| Symbol lookup | `mcp__gitnexus__query` | Finding where a symbol, function, class, or pattern is defined or used across the repo |
| Full symbol context | `mcp__gitnexus__context` | 360° view of a symbol — callers, callees, type references, and process participation |
| Blast-radius analysis | `mcp__gitnexus__impact` | Understanding which symbols and files break if you change or delete a given symbol |
| Change mapping | `mcp__gitnexus__detect_changes` | Map a git diff (or list of changed files) to the symbols and processes it affects |
| Repo discovery | `mcp__gitnexus__list_repos` | Discover which repos are indexed and available for cross-repo queries |
| Cross-repo flow | `mcp__gitnexus__group_query` | Trace an execution flow or dependency chain across multiple indexed repos |

### Lookup priority rule

1. **Use gitnexus first** for any structural code question: symbol definitions, call graphs, impact analysis, cross-file dependencies.
   - Start with `mcp__gitnexus__query` to locate a symbol.
   - Deepen with `mcp__gitnexus__context` when you need callers, callees, or type relationships.
   - Run `mcp__gitnexus__impact` before any refactor or deletion to compute blast radius.
2. **Fall back to `grep` or `Read`** only when:
   - GitNexus returns no results for the query.
   - The GitNexus MCP is unavailable for the run (no `mcp__gitnexus__*` tools in scope).
   - The question is about raw file content, not about code structure (e.g. reading a config file, checking a comment).
3. **Never open an entire file** just to find a symbol when gitnexus can answer it directly.
4. **Never skip gitnexus** when the tools are available and the question is structural.

### When gitnexus is available

The `mcp__gitnexus__*` tools appear in your tool list when `GITNEXUS_MCP_URL` is set in the executor environment. If the tools are absent, fall back to grep/read without blocking — the indexer may not have completed a cycle yet.

### TypeScript and Python priority

GitNexus supports multiple languages; TypeScript and Python are the primary targets for this workspace. For other languages, gitnexus may return partial results — verify with grep if the coverage seems incomplete.
