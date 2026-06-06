-include .env
export
PORT   ?= 8000

.PHONY: help install run dev
help:
	@echo "Usage: make <target>"
	@echo ""
	@echo "Local"
	@echo "  install   Install dependencies"
	@echo "  dev       Start with auto-reload"
	@echo "  run       Start in production mode"

install:
	uv pip install -e "../[workflow-gateway]"

dev:
	cd .. && uv run uvicorn workflow_gateway.app:app --host 0.0.0.0 --port $(PORT) --reload --reload-dir workflow_gateway

run:
	cd .. && uv run uvicorn workflow_gateway.app:app --host 0.0.0.0 --port $(PORT)