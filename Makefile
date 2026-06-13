-include .env
export
PORT   ?= 8000

.PHONY: help submodules install run dev
help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "  submodules  Sync git submodules to the pinned commits"
	@echo "  install     Install dependencies (gateway + vendored hermes-agent)"
	@echo "  dev         Start with auto-reload"
	@echo "  run         Start in production mode"

submodules:
	scripts/sync-submodules.sh

install: submodules
	uv pip install -e "./vendor/hermes-agent[anthropic]"
	uv pip install -e ".[workflow-gateway]"

dev:
	uv run uvicorn src.app:app --host 0.0.0.0 --port $(PORT) --reload --reload-dir src

run:
	uv run uvicorn src.app:app --host 0.0.0.0 --port $(PORT)
