# ForgeFlow task commands. On Windows without `make`, run the commands shown in README.md.

.PHONY: install dev-api dev-worker dev-agent-worker dev-frontend test lint format typecheck compose-up compose-down logs

install:
	uv sync
	cd apps/frontend && npm install

dev-api:
	uv run uvicorn forgeflow.apps.api.main:app --reload --port 8000

dev-worker:
	uv run python -m forgeflow.apps.worker.main

dev-agent-worker:
	uv run python -m forgeflow.apps.agent_worker.main

dev-frontend:
	cd apps/frontend && npm run dev

test:
	uv run pytest

lint:
	uv run ruff check src tests
	cd apps/frontend && npm run typecheck

format:
	uv run ruff format src tests

typecheck:
	uv run mypy

compose-up:
	docker compose up --build -d

compose-down:
	docker compose down

logs:
	docker compose logs -f platform-api orchestrator-worker agent-worker
