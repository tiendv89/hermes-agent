---
name: init-agent
description: Set up a new local agent compute environment — configures Docker Compose, .env, agent.yaml, and the RAG stack (qdrant, rag-server, indexer) which is enabled by default.
---

## Purpose

Guide an operator through the one-time setup of a local agent host using Docker Compose.
This skill is the canonical setup path for new agent deployments.

Scope: agent compute (Docker Compose stack). For workspace setup (management repo, feature
tracking), use `init-workspace` instead.

---

## Prerequisites

Verify these before starting. Stop if any are missing and tell the operator what is needed.

| Prerequisite | How to check |
|---|---|
| Docker 24+ with Buildx | `docker buildx version` |
| Docker Compose v2 | `docker compose version` |
| Anthropic API key | `console.anthropic.com` |
| GitHub PAT with `repo` scope | `github.com/settings/tokens` |
| SSH key with read/write access to workspace repos | `ssh -T git@github.com` |

---

## Step 1 — Locate the compose directory

```bash
# From the workflow/ repo root:
cd runtime/orchestrator/templates
```

All subsequent steps run from this directory.

---

## Step 2 — Configure `.env`

```bash
cp .env.example .env
```

Collect and write the following values. Ask one group at a time.

### Required

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (`sk-ant-...`) |
| `GITHUB_TOKEN` | GitHub PAT with `repo` scope (`ghp_...`) |
| `GIT_AUTHOR_EMAIL` | Email for git identity (task log attribution) |
| `GIT_AUTHOR_NAME` | Name for git identity |
| `WORKFLOW_URL` | SSH URL of the workflow repo (cloned at startup) |

### SSH key delivery (choose one)

**Option A — env var (preferred for CI/K8s):**
```env
SSH_PRIVATE_KEY=-----BEGIN OPENSSH PRIVATE KEY-----
...key content...
-----END OPENSSH PRIVATE KEY-----
```

**Option B — file mount (Docker Compose local use):**
```env
SSH_KEY_DIR=~/.ssh
```

---

## Step 3 — Configure `agent.yaml`

```bash
cp agent.yaml.example agent.yaml
```

Collect and write:

| Field | Description |
|---|---|
| `watches` | SSH URL(s) of the workspace management repos this agent should pick tasks from |
| `jitter_max_seconds` | Pre-claim jitter; increase when running many agents (e.g. `15`) |
| `budget.max_tokens_per_task` | Token budget per task (default: `100000`) |
| `budget.max_iterations` | Iteration cap per task (default: `50`) |
| `idle_sleep_seconds` | Poll interval in seconds (default: `60`; set `0` for single-shot mode) |

---

## Step 4 — RAG stack (optional, enabled by default)

The RAG stack (qdrant, rag-server, indexer) is included in the default compose setup
and gives agents searchable project knowledge at claim time. All three services start
with the standard `docker compose up --build` — no extra flags needed.

Inform the operator: _"The RAG context stack is enabled by default. It requires a local
clone of the `rag-service` repo. If you don't have one or want to skip RAG for now,
you can comment out the qdrant, rag-server, and indexer services in docker-compose.yml."_

Continue to Step 4a to configure the RAG variables. If the operator chooses to skip
RAG entirely, jump to Step 5 and leave the RAG variables unset.

### Step 4a — RAG prerequisites

| Prerequisite | Description |
|---|---|
| `rag-service` repo cloned locally | The rag-server and indexer images are built from this repo |
| Workspace management repo cloned locally | Contains `workspace.yaml` — the indexer reads it to discover repos |

Ask:
- **RAG_SERVICE_LOCAL_PATH** — local filesystem path to the `rag-service` repo clone
- **WORKSPACE_MGMT_LOCAL_PATH** — local filesystem path to the workspace management repo clone (the one containing `workspace.yaml`)

### Step 4b — RAG environment variables

Add to `.env`:

```env
# RAG stack
RAG_SERVICE_LOCAL_PATH=/path/to/your/rag-service

# Path to your local workspace management repo clone (contains workspace.yaml).
# The indexer mounts workspace.yaml read-only from this directory.
WORKSPACE_MGMT_LOCAL_PATH=/path/to/your/project-workspace

# Workspace ID — must match workspace_id in your workspace.yaml (e.g. workspace)
WORKSPACE_ID=workspace

# Optional overrides (defaults shown):
# QDRANT_URL=http://qdrant:6333
# MCP_RAG_URL=http://rag-server:8000
# INDEXER_POLL_INTERVAL_SECONDS=300
```

Explain to the operator:
- `WORKSPACE_ID` isolates this workspace's index in Qdrant — multiple workspaces can share one Qdrant instance
- `WORKSPACE_MGMT_LOCAL_PATH` is used to mount `workspace.yaml` into the indexer container (read-only). The indexer reads `workspace.yaml` at startup and discovers all repos from it, cloning each via SSH. No per-repo volume mounts are needed.
- `MCP_RAG_URL` is passed to agents so they can call `rag_query` mid-task; the default `http://rag-server:8000` works when the full stack runs in the same compose project
- `SSH_PRIVATE_KEY` (already set in `.env` for agent SSH access) is also passed to the indexer for cloning repos — no additional key configuration is needed

---

## Step 5 — Start the stack

```bash
docker compose up --build
```

This starts agents plus the full RAG stack (qdrant, rag-server, indexer) by default.

**First-start note:** The `rag-server` and `indexer` images download
`sentence-transformers/all-MiniLM-L6-v2` (~90 MB) on their first build. Subsequent
starts are fast. The index is empty until the indexer completes its first polling cycle
(up to `INDEXER_POLL_INTERVAL_SECONDS` = 5 min by default). Agents degrade gracefully
during this warm-up period.

---

## Step 6 — Verify

Run these checks after the stack starts:

```bash
# All containers running?
docker compose ps

# Agent picking up work?
docker compose logs agent-1 | tail -20

# RAG server reachable?
curl -s http://localhost:8000/health | jq .

# Qdrant reachable?
curl -s http://localhost:6333/healthz
```

Expected output:
- `agent-1` shows `bootstrap_started` then `poll_workspace_pulled` in JSON logs
- `rag-server` health endpoint returns `{"status": "ok"}`
- `qdrant` healthz returns `healthz check passed`

---

## Step 7 — Add a second agent (optional)

Uncomment the `agent-2` block in `docker-compose.yml`, then re-run:

```bash
docker compose up --build
```

Both agents share the same `workspaces` volume and use the same Qdrant collection —
they do not diverge.

---

## Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| `RAG_SERVICE_LOCAL_PATH must be set` error | RAG_SERVICE_LOCAL_PATH missing from .env | Add it to .env and re-run |
| `WORKSPACE_MGMT_LOCAL_PATH must be set` error | WORKSPACE_MGMT_LOCAL_PATH missing from .env | Add it to .env — must point to your local workspace management repo clone |
| `WORKSPACE_ID must be set` error | WORKSPACE_ID missing from .env | Add it (must match workspace.yaml workspace_id) |
| `rag-server` fails on startup | Qdrant not yet ready | Wait 10–15 s; rag-server retries automatically |
| Indexer fails to clone repos | SSH key not set or wrong key | Ensure `SSH_PRIVATE_KEY` is set in .env with a key that has read access to all repos in workspace.yaml |
| Agents log `MCP_RAG_URL` connection refused | rag-server not yet ready | Wait for rag-server to finish starting; or set `MCP_RAG_URL=` in .env to disable RAG |
| `bootstrap_failed: git_workspace_sync_failed` | SSH key can't reach workspace repo | Verify SSH key in .env; check `ssh -T git@github.com` |
| `skill_reference_audit` warnings | A skill slug in tasks.md has no matching directory | Non-fatal — agent skips that task |

---

## Stopping and resetting

```bash
# Stop all containers (keep volumes):
docker compose down

# Stop and wipe all volumes (forces full re-clone and re-index on next start):
docker compose down -v
```

---

## Production deployment

The local Docker Compose is for development only. Kubernetes and GitHub Actions
deployment templates have been removed as part of the orchestrator/executor split
(see `agent-runtime-split` feature). Fresh templates targeting the new
`runtime/orchestrator/` structure will be added in a follow-on feature when
K8s or CI deployment is actually needed.

In production with Docker Compose:
- Replace `QDRANT_URL=http://qdrant:6333` with your Qdrant VM address (`http://<vm-ip>:6333`)
- Replace `MCP_RAG_URL=http://rag-server:8000` with your rag-server pod address
- The rag-server and indexer images should be pre-built and pushed to a registry
- No code change is required between local and production — only `QDRANT_URL` and `MCP_RAG_URL` change
