-include .env
export
PORT ?= 8000

.PHONY: help submodules update-submodules install lint test run dev \
	install-opencode opencode-serve
help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "  submodules         Sync git submodules to the pinned commits"
	@echo "  update-submodules  Pull submodules to latest upstream and commit"
	@echo "  install            Install dependencies (gateway + vendored hermes-agent)"
	@echo "  lint               Run ruff over the project (vendor excluded)"
	@echo "  test               Run the test suite"
	@echo "  dev                Start with auto-reload (PORT=$(PORT); override e.g. PORT=8010 make dev)"
	@echo "  run                Start in production mode (same PORT override)"
	@echo "  install-opencode   Install the pinned opencode server (vendor/opencode, pnpm)"
	@echo "  opencode-serve     Run the opencode server on 0.0.0.0:4096 (non-Docker dev)"
	@echo ""
	@echo "One process serves both the workflow (web chat) and coding (IDE)"
	@echo "profiles — see src/app.py. There is no more per-profile split."

lint:
	uvx ruff check .

test:
	env -u WORKFLOW_BACKEND_URL -u WORKFLOW_BACKEND_SERVICE_TOKEN \
	    -u USER_SERVICE_URL -u USER_SERVICE_TOKEN \
	    -u STORAGE_SERVICE_URL -u STORAGE_SERVICE_TOKEN \
	    -u NOTIFICATION_SERVICE_URL -u NOTIFICATION_SERVICE_TOKEN \
	    -u GITHUB_TOKEN -u GATEWAY_SERVICE_TOKEN \
	    -u GITNEXUS_MCP_URL -u RAG_MCP_URL -u FIRECRAWL_API_KEY \
	    uv run pytest tests/ -q

submodules:
	scripts/sync-submodules.sh

update-submodules:
	git submodule update --remote --merge vendor/hermes-agent
	git add vendor/hermes-agent
	git diff --cached --quiet || git commit -m "chore: update hermes-agent submodule to latest"

install: submodules
	uv pip install -e "./vendor/hermes-agent[anthropic]"
	uv pip install -e ".[workflow-gateway]"

dev:
	uv run uvicorn src.app:app --host 0.0.0.0 --port $(PORT) --reload --reload-dir src

run:
	uv run uvicorn src.app:app --host 0.0.0.0 --port $(PORT)

install-opencode:
	cd vendor/opencode && pnpm install

opencode-serve:
	cd vendor/opencode && pnpm exec opencode serve --hostname 0.0.0.0 --port 4096 --print-logs
