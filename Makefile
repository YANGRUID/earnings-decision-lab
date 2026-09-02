.PHONY: db-up db-down preflight migrate migrate-new test lint fmt eval

db-up:
	docker compose up -d db

db-down:
	docker compose down

# Phase 4 deployment-safety hardening (2026-08-26), Section 40 -- real disk-
# space check before a migration or image rebuild; see scripts/deploy_
# preflight.sh's own docstring for the real incident this exists because of.
preflight:
	./scripts/deploy_preflight.sh

migrate: preflight
	cd backend && uv run alembic upgrade head

migrate-new:
	cd backend && uv run alembic revision --autogenerate -m "$(m)"

test:
	cd backend && uv run pytest

lint:
	cd backend && uv run ruff check src tests

fmt:
	cd backend && uv run ruff format src tests

eval:
	cd backend && uv run python ../evaluation/scripts/run_all.py
