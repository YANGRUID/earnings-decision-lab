# Backend

FastAPI service for Earnings Decision Lab: data providers, ingestion, deterministic financial
analytics, hybrid RAG, structured extraction, and agent tool orchestration. See
[../docs/architecture.md](../docs/architecture.md) for the full system design.

## Local development

```bash
cp ../.env.example ../.env   # fill in real values — see docs/data_sources.md, docs/llm_providers.md
make -C .. db-up             # starts Postgres + pgvector via docker-compose.yml
uv sync
uv run alembic upgrade head
uv run uvicorn api.main:app --reload --port 8000
```

## Tests, lint, types

```bash
uv run pytest          # 243 tests — unit, API, provider (mocked), RAG, agent, evaluation-metrics
uv run ruff check src tests
uv run mypy src
```

No test in this suite makes a real network call or spends real money — provider/LLM calls are
mocked (`pytest-httpx`, stub providers). See [../docs/evaluation.md](../docs/evaluation.md) for
the separate evaluation scripts that *do* make real, billed LLM calls on purpose.

## Docker

`Dockerfile` is a multi-stage build (see comments in the file for why `--no-editable` and the
embedding-model cache pre-warming matter) — run the whole stack via `docker compose up --build`
from the repo root rather than building this image standalone.

## Layout

```
src/
  api/            FastAPI routers, DI, middleware, exception handling
  core/           settings (pydantic-settings)
  db/             SQLAlchemy session + Alembic migrations
  models/         ORM models (12 tables)
  schemas/        Pydantic schemas (API responses, structured LLM extraction/agent output)
  providers/      market/earnings/filings adapters (SEC EDGAR, Tiingo, Alpha Vantage) + fixtures
  ingestion/      one-off and backfill data-loading scripts
  analytics/      options payoff/Greeks/implied-move + earnings surprise/IV-crush (deterministic)
  rag/            document parsing, chunking, embeddings, hybrid retrieval, answer synthesis
  agents/         intent classification, planning, tool execution, verification
  prompts/        versioned prompt templates
  services/       LLM provider abstraction, structured extraction
  evaluation/     pure metric functions + typed dataset/result models (see ../evaluation/)
  observability/  structured JSON logging, HTTP call latency logging, credential redaction
tests/
```
