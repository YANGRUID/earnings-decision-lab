.PHONY: db-up db-down migrate migrate-new test lint fmt

db-up:
	docker compose up -d db

db-down:
	docker compose down

migrate:
	cd backend && uv run alembic upgrade head

migrate-new:
	cd backend && uv run alembic revision --autogenerate -m "$(m)"

test:
	cd backend && uv run pytest

lint:
	cd backend && uv run ruff check src tests

fmt:
	cd backend && uv run ruff format src tests
