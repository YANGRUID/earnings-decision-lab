# Backend

FastAPI service for Earnings Decision Lab: data providers, ingestion, deterministic financial
analytics, RAG, and agent orchestration.

Not yet implemented — this is a Phase 0 skeleton. See [../docs/architecture.md](../docs/architecture.md).

## Planned layout

```
src/
  api/            FastAPI routers
  core/           settings, logging, exception handling
  db/             SQLAlchemy session + Alembic migrations
  models/         ORM models
  schemas/        Pydantic schemas
  providers/      market/options/earnings/filings/transcript adapters
  ingestion/      scheduled snapshot collection
  analytics/      options + earnings deterministic calculations
  rag/            document parsing, chunking, hybrid retrieval
  agents/         intent classification, planner, tool orchestration
  prompts/        versioned prompt templates
  services/       cross-cutting application services
  observability/  structured logging, tracing, cost tracking
tests/
```
