.PHONY: up down test lint api web migrate
up:
	docker compose up --build -d
down:
	docker compose down
migrate:
	.venv/bin/alembic upgrade head
api:
	.venv/bin/uvicorn agentbenchx.main:app --reload --host 127.0.0.1 --port 8000
web:
	cd apps/web && npm run dev
test:
	.venv/bin/python scripts/test_integration.py
lint:
	.venv/bin/ruff check apps/api workers scripts tests
	cd apps/web && npm run typecheck
