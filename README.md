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
| `WORKFLOW_BACKEND_URL` / `WORKFLOW_BACKEND_SERVICE_TOKEN` | yes | workflow-backend HTTP API base URL + shared service token |
| `ANTHROPIC_API_KEY` | yes | Anthropic API key (used when `HERMES_PROVIDER=anthropic`) |
| `GITHUB_TOKEN` | write tools | PAT with `contents:write` — for artifact write tools |
| `GATEWAY_SERVICE_TOKEN` | no | Shared token gating BFF-injected identity headers; unset = local/direct mode |
| `HERMES_MODEL` | no | Model (default: `claude-sonnet-4-6`) |
| `HERMES_PROVIDER` | no | Provider (default: `anthropic`) |
| `GITNEXUS_MCP_URL` / `RAG_MCP_URL` | no | MCP endpoints used by plugin tools |
| `OPENCODE_SERVER_URL` | coding chat | Base URL of the opencode server (see below), e.g. `http://localhost:4096` |
| `OPENCODE_SERVER_PASSWORD` | no | opencode server auth; omit for a server unsecured behind this gateway's own network perimeter |

## Run locally

```bash
# 1. Start Postgres + opencode (compose brings them up on localhost:25434 / :4096)
docker compose up -d

# 2. Make sure DATABASE_URL in .env points at that Postgres, then:
make dev          # auto-reload on port 8000
# or
make run          # production mode
```

opencode powers coding-verdict turns on `/coding/chat` (see `src/services/opencode_client.py`).
To run it outside Docker instead:

```bash
make install-opencode   # pnpm install into vendor/opencode
make opencode-serve     # opencode serve --hostname 0.0.0.0 --port 4096
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
| `make update-submodules` | Pull submodules to latest upstream and commit |
| `make install` | Install the vendored agent + gateway dependencies |
| `make lint` | Run ruff over the project (vendor excluded) |
| `make dev` | Start with auto-reload |
| `make run` | Start in production mode |
| `make install-opencode` | Install the pinned opencode server (`vendor/opencode`, pnpm) |
| `make opencode-serve` | Run the opencode server on `0.0.0.0:4096` (non-Docker dev) |

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

```bash
make update-submodules
```

This fetches the latest upstream commit, stages the pointer change, and commits it. Re-run `make install` afterwards to pick up any new dependencies, and `make lint` to catch any API drift.
