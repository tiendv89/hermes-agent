# hermes-workflow-gateway

FastAPI **workflow gateway** and Hermes **workflow plugin** for the digital-factory
M3 agent chat. This repository contains only the workflow-specific code; the
upstream [Hermes Agent](https://github.com/nousresearch/hermes-agent) codebase is
vendored as a git submodule and consumed as a dependency.

## Layout

```
.
├── vendor/hermes-agent/   # git submodule → nousresearch/hermes-agent (pinned)
├── workflow_gateway/      # FastAPI gateway: sessions, SSE streaming, Postgres store
├── workflow_plugin/       # Hermes plugin: tools, hooks, RAG/artifact/task helpers
├── hermes_home/           # config.yaml mounted into the agent home (~/.hermes)
├── tests/                 # workflow_gateway + workflow_plugin test suites
└── pyproject.toml         # depends on hermes-agent via [tool.uv.sources] path
```

The gateway and plugin import upstream modules directly (`run_agent`,
`hermes_state`, `hermes_cli.plugins`), so `hermes-agent` is installed from the
submodule as an editable path dependency.

## Setup

Clone with the submodule:

```bash
git clone --recurse-submodules <this-repo-url>
# or, if already cloned:
git submodule update --init --recursive
```

Install (uv resolves `hermes-agent` from `vendor/hermes-agent`):

```bash
uv sync --extra workflow-gateway
# or with pip:
pip install -e ".[workflow-gateway]"
```

## Run the gateway

```bash
uvicorn workflow_gateway.app:app --host 0.0.0.0 --port 8000
```

See `workflow_gateway/README.md` for gateway configuration and
`.env.example` for the workflow-specific environment variables.

## Updating the vendored agent

The submodule is pinned to a specific upstream commit. To move to a newer one:

```bash
cd vendor/hermes-agent
git fetch origin
git checkout <new-commit-or-tag>
cd ../..
git add vendor/hermes-agent
git commit -m "chore: bump hermes-agent submodule"
```

Re-run the test suite after bumping — the workflow code depends on upstream
module APIs that can change between commits.
