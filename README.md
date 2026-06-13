# hermes-workflow-gateway

FastAPI gateway and Hermes plugin that wrap the [Hermes Agent](https://github.com/nousresearch/hermes-agent)
as a workspace-aware AI agent, exposing an SSE chat API consumed by digital-factory-ui.

The upstream agent is vendored as a git submodule at `vendor/hermes-agent` and
consumed as an editable dependency; this repo contains only the gateway (`src/`)
and the workflow plugin (`plugins/`).

## Requirements

- Python 3.11–3.13
- PostgreSQL
- `uv`
- `git` (for the submodule)

## Setup

```bash
# Clone with the vendored agent:
git clone --recurse-submodules <repo-url>
# (already cloned? pull the submodule in:)
make submodules

# Configure and install:
cp .env.example .env       # then fill in the values
make install               # installs the vendored agent + gateway
```

## Environment variables

See `.env.example` for the full list. Key variables:

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection string for the gateway session store |
| `WORKFLOW_DATABASE_URL` | yes | Postgres connection string for the workflow-backend DB |
| `ANTHROPIC_API_KEY` | yes | Anthropic API key (used when `HERMES_PROVIDER=anthropic`) |
| `GITHUB_TOKEN` | write tools | PAT with `contents:write` — for artifact write tools |
| `GATEWAY_SERVICE_TOKEN` | no | Shared token gating BFF-injected identity headers; unset = local/direct mode |
| `HERMES_MODEL` | no | Model (default: `claude-sonnet-4-6`) |
| `HERMES_PROVIDER` | no | Provider (default: `anthropic`) |
| `GITNEXUS_MCP_URL` / `RAG_MCP_URL` | no | MCP endpoints used by plugin tools |

## Run locally

```bash
# 1. Start Postgres (compose brings it up on localhost:25434)
docker compose up -d

# 2. Make sure DATABASE_URL in .env points at that Postgres, then:
make dev          # auto-reload on port 8000
# or
make run          # production mode
```

Migrations in `migrations/` are applied automatically on startup.

## Run with Docker

```bash
docker build -t hermes-workflow-gateway .
docker run --rm -p 8000:8000 --env-file .env hermes-workflow-gateway
```

## Makefile targets

| Target | Description |
|---|---|
| `make submodules` | Sync git submodules to the pinned commits |
| `make install` | Install the vendored agent + gateway dependencies |
| `make lint` | Run ruff over the project (vendor excluded) |
| `make dev` | Start with auto-reload |
| `make run` | Start in production mode |

## API

All routes are mounted at `/api/v1`. The caller identity comes from the
BFF-injected `X-User-Id` header (gated by the shared service token), not the
request body.

### `POST /api/v1/session`

**Body**
```json
{ "workspace_id": "my-workspace", "feature_id": "search" }
```

**Response**
```json
{ "session_id": "sess_abc123..." }
```

### `POST /api/v1/chat`

**Body**
```json
{
  "session_id": "sess_abc123...",
  "message": "Draft a product spec for the search feature",
  "workspace_id": "my-workspace",
  "feature_id": "search"
}
```

Response is an SSE stream.

**Event types**

| Type | Payload | Description |
|---|---|---|
| `message_output_partial` | `{ content }` | Streamed text delta |
| `tool_call_item` | `{ call_id, name, status }` | Tool invocation started |
| `function_call_output` | `{ call_id, name, output }` | Tool result |
| `artifact_saved` | `{ artifact }` | Write tool succeeded |
| `usage` | `{ input, output, cached }` | Token counts at turn end |
| `error` | `{ message }` | Stream error |
| `[DONE]` | — | End of stream |

### `GET /health`

Returns `{ "status": "ok" }`.

## Project structure

```
src/                  — FastAPI gateway package (uvicorn src.app:app)
  app.py              — app factory + lifespan (DB pool, migrations)
  api/router.py       — route handlers
  db/store.py         — Postgres session/message CRUD + migration runner
  streaming/sse.py    — AIAgent callbacks → SSE translation
plugins/              — hermes workflow plugin (tools, hooks, context)
migrations/           — SQL migrations, applied on startup
  001_initial_schema.sql
configs/              — agent home (config.yaml), copied to ~/.hermes in Docker
vendor/hermes-agent/  — upstream agent (git submodule, editable dependency)
```

## Updating the vendored agent

The submodule is pinned to a specific upstream commit. To advance it:

```bash
scripts/sync-submodules.sh --remote     # fetch + move to upstream latest
git add vendor/hermes-agent && git commit -m "chore: bump hermes-agent submodule"
```

Re-run `make lint` and the test suite after bumping — the gateway depends on
upstream module APIs that drift between commits.
