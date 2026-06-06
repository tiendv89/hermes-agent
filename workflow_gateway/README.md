# workflow_gateway

FastAPI server that wraps hermes as a workspace-aware AI agent, exposing an SSE chat API consumed by digital-factory-ui.

## Requirements

- Python 3.13+
- PostgreSQL
- `uv`

## Setup

```bash
cp .env.example .env
# fill in the values in .env
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | yes | Postgres connection string |
| `ANTHROPIC_API_KEY` | yes | Anthropic API key |
| `WORKFLOW_BACKEND_URL` | yes | Base URL of workflow-backend |
| `GITHUB_TOKEN` | write tools | PAT with `contents:write` — for artifact writes |
| `HERMES_MODEL` | no | Model (default: `claude-sonnet-4-6`) |
| `HERMES_PROVIDER` | no | Provider (default: `anthropic`) |

## Run locally

```bash
# 1. Start Postgres
docker run -d --name hermes-agent-db \
  -e POSTGRES_USER=hermes \
  -e POSTGRES_PASSWORD=hermes \
  -e POSTGRES_DB=hermes_gateway \
  -p 5432:5432 postgres:16

# 2. Install and start
make install
make dev
```

Migrations run automatically on startup.

## Run with Docker Compose

```bash
cd workflow_gateway

make up       # start gateway + postgres
make logs     # follow gateway logs
make restart  # rebuild and restart
make down     # stop everything
```

Postgres data is persisted in the `postgres_data` Docker volume.

## Makefile targets

| Target | Description |
|---|---|
| `make install` | Install Python dependencies |
| `make dev` | Start with auto-reload |
| `make run` | Start in production mode |
| `make up` | Docker Compose — start all services |
| `make down` | Docker Compose — stop all services |
| `make logs` | Follow gateway container logs |
| `make restart` | Rebuild image and restart gateway |
| `make shell` | Shell inside gateway container |
| `make db-shell` | psql inside postgres container |

## API

All routes are mounted at `/api/v5`.

### `POST /api/v5/create_session`

**Body**
```json
{ "user_id": "user_123" }
```

**Response**
```json
{ "session_id": "sess_abc123..." }
```

### `POST /api/v5/stream_chat`

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
workflow_gateway/
  app.py              — app factory + lifespan (DB pool, migrations)
  api/
    router.py         — route handlers
  sessions/
    store.py          — Postgres session/message CRUD + migration runner
  streaming/
    sse.py            — AIAgent callbacks → SSE translation
  migrations/
    001_initial_schema.sql
```
