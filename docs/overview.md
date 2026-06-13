# Overview

## 1. Project Summary
- **Workspace ID**: `hermes-agent`
- **Project Name**: Hermes Workflow Gateway
- **Purpose**:
  A workspace-aware AI agent gateway for the digital-factory / M3 workflow. It
  runs agent turns over HTTP (streaming SSE chat), persists transcripts, and
  exposes the workflow plugin — workspace/feature context tools, the document
  write pipeline, and the stage-review lifecycle. It also ships the bundled
  skills library so the agent authors product specs, technical designs, and
  task breakdowns consistently.

## 2. Roles in this Workspace
- **tech_lead** — owns the management repo, technical designs, and task breakdowns.
- **backend_engineer** — owns the gateway service (`hermes-agent`).

## 3. Repositories
Use `workspace.yaml` as the source of truth. Summary for humans:
- **management-repo** — feature docs, task YAMLs, workspace `CLAUDE.md`.
- **hermes-agent** — the workflow gateway: a Python / FastAPI service built on
  the vendored `hermes-agent` runtime (`src/` app + `plugins/` workflow plugin).

## 4. High-Level Architecture
- **`src/`** — FastAPI app (`src/app.py`) and the API router (`src/api/router.py`):
  session lifecycle, streaming chat, document save, tool registry, stage transitions.
- **`src/streaming/`** — bridges the agent's threaded callbacks to an SSE stream.
- **`src/db/`** — Postgres session/transcript store and the `GatewaySessionDB`
  proxy that mirrors agent writes.
- **`plugins/`** — the workflow plugin: workspace/feature tools, artifact write
  + edit tools, tasks, RAG/gitnexus MCP bridges, approval, and the bundled
  **skills** subsystem (`plugins/skills/`: `technical_skills/`,
  `workflow_skills/`, `shared.md`).
- **`vendor/hermes-agent/`** — the upstream agent runtime (submodule).

## 5. Environments
- develop — enabled
- staging — enabled
- production — enabled

## 6. Automation Policy
- Agents draft `product-spec.md`, `technical-design.md`, and task breakdowns;
  humans approve or reject each stage via the stage-transition API.
- Document writes go through the gateway's write pipeline (optimistic-locked,
  committed to the feature branch); humans can also save edits directly.
- Stage approvals/rejections/reopens are recorded in `status.yaml` with a
  review history. Final handoff requires human approval.
