---
name: gitnexus-mcp
description: Code-graph intelligence via GitNexus. Use for structural code lookups — symbol definitions, call graphs, impact analysis, and cross-repo execution tracing — before falling back to grep or file reads.
---

## GitNexus — code-graph tool

GitNexus structural code intelligence is exposed through a **single tool, `query_gitnexus`**, with a `tool=` selector and a `repo=` argument. (There is no `mcp__gitnexus__*` tool namespace — always call `query_gitnexus`.) It operates on a pre-built AST + call-graph index and answers questions that grep and file reads cannot: "what calls this function", "what breaks if I delete this", "trace this execution path end-to-end."

**GitNexus is the source of truth for which repos exist.** Discover the repo from `list_repos`; it does NOT need to be registered in `workspace.yaml`.

### Tool reference

All operations are issued as `query_gitnexus(tool="<op>", ...)`:

| Operation | Call | When to use |
|---|---|---|
| Repo discovery | `query_gitnexus(tool="list_repos")` | FIRST step — discover which repos are indexed; pick the repo name to pass below. No `query`/`repo`. |
| Symbol / flow lookup | `query_gitnexus(query="<name>", tool="query", repo="<r>")` | Find where a symbol/function/class is defined or used, or a keyword flow |
| Full symbol context | `query_gitnexus(query="<name>", tool="context", repo="<r>")` | 360° view of a symbol — callers, callees, type references, process participation |
| Blast-radius analysis | `query_gitnexus(query="<name>", tool="impact", repo="<r>")` | What breaks if you change/delete a symbol (`direction="upstream"` = dependents) |
| Change mapping | `query_gitnexus(tool="detect_changes", repo="<r>")` | Map the current uncommitted git diff to affected symbols/flows (no `query`) |

**`repo=` is required** on `query`/`context`/`impact`/`detect_changes` once more than one repo is indexed — the server rejects the call with `'repo' is a required property` otherwise. Omit `repo` only when `list_repos` shows a single indexed repo. For cross-repo tracing, pass group mode `repo="@<groupName>"`.

### Lookup priority rule

1. **`list_repos` first**, then **use GitNexus** for any structural code question, passing `repo=`:
   - Start with `tool="query"` to locate a symbol.
   - Deepen with `tool="context"` when you need callers, callees, or type relationships.
   - Run `tool="impact"` before any refactor or deletion to compute blast radius.
2. **Fall back to `grep` or `Read`** only when:
   - GitNexus returns no results for the query (a `'repo' is a required property` error is NOT "no results" — retry with the repo name from `list_repos`).
   - `query_gitnexus` is unavailable for the run (not in the tool list).
   - The question is about raw file content, not code structure (e.g. reading a config file, checking a comment).
3. **Never open an entire file** just to find a symbol when GitNexus can answer it directly.
4. **Never skip GitNexus** when the tool is available and the question is structural.
5. **Never block on `workspace.yaml`** — if `list_repos` shows the repo, query it.

### When GitNexus is available

`query_gitnexus` appears in your tool list when `GITNEXUS_MCP_URL` is set in the executor environment. If it is absent, fall back to grep/read without blocking — the indexer may not have completed a cycle yet.

### TypeScript and Python priority

GitNexus supports multiple languages; TypeScript and Python are the primary targets for this workspace. For other languages, GitNexus may return partial results — verify with grep if the coverage seems incomplete.
