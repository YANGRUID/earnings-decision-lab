# Architecture (working document)

This document is updated as each phase lands. It currently reflects **Phase 7**: the
PostgreSQL schema (12 tables) is live via Alembic migrations; SEC EDGAR and Tiingo/Alpha
Vantage (fallback-chained) are real, wired-up data providers; 150 real earnings events are
seeded for NVDA/AMD/MU/SNDK; deterministic options and IV-crush/event-replay engines are
implemented and unit-tested; a provider-agnostic LLM layer sits under a real, working
hybrid-RAG pipeline (2,231 chunks from 93 real SEC filings); structured guidance extraction
runs against real filing text; and an explicit agent orchestrator (intent classification,
provider-capability-aware planning, tool execution, evidence collection, cited synthesis, and
a separate verification step with bounded revision) ties all of it together behind seven real
tools, verified live end-to-end against DeepSeek. Details: [data_model.md](data_model.md),
[data_sources.md](data_sources.md), [options_methodology.md](options_methodology.md),
[earnings_methodology.md](earnings_methodology.md), [llm_providers.md](llm_providers.md),
[ai_architecture.md](ai_architecture.md), [limitations.md](limitations.md).
No API or frontend exists yet — Phase 8 (FastAPI + React) is next, exposing this orchestrator
and the underlying data through a real interface.

## Goal

Help answer questions like *"what is the market pricing in before MU earnings, and how does
that compare with history?"* by combining:

1. **Deterministic financial analytics** in Python (surprise, implied/realised move, IV crush,
   options payoffs) — never delegated to an LLM.
2. **Point-in-time structured data** (earnings snapshots, options snapshots, price reactions)
   with no lookahead bias.
3. **Evidence-grounded AI research** (RAG over SEC filings / earnings materials, tool-calling
   agent orchestration) with citations back to source documents.

## Scope (V1)

Four tickers only: **NVDA, AMD, MU, SNDK**. The data model and provider interfaces are designed
to make adding tickers later a config change, not a rewrite — but V1 intentionally does not
attempt full US-equity coverage. See [engineering_decisions.md](engineering_decisions.md)
(added in a later phase) for the reasoning.

## Five core modules

| # | Module | Responsibility |
|---|--------|-----------------|
| 1 | Earnings Expectations | Pre-earnings point-in-time snapshot: consensus estimates, implied move, IV term structure, positioning |
| 2 | Earnings Outcomes | Post-earnings actuals, surprise, guidance change, price reaction, implied-vs-realised error |
| 3 | Options & Volatility Analytics | Deterministic strategy payoffs, implied move methodology, IV crush analysis |
| 4 | Historical Event Replay | Rule-based strike selection and payoff reconstruction across past earnings events |
| 5 | AI Research Assistant | Hybrid RAG + tool-calling agent over filings/transcripts and the modules above |

## Planned system layout

```
backend/
  src/
    api/            FastAPI routers (typed request/response schemas)
    core/            settings, logging, exception handling
    db/              SQLAlchemy engine/session, Alembic migrations
    models/          ORM models (company, earnings_event, snapshots, ...)
    schemas/         Pydantic schemas (API + structured LLM extraction)
    providers/       MarketDataProvider / OptionsDataProvider / EarningsDataProvider /
                      FilingsProvider / TranscriptProvider adapters
    ingestion/        scheduled snapshot collection (APScheduler)
    analytics/
      options/       payoff, implied move, Greeks
      earnings/      surprise, IV crush, implied-vs-realised
    rag/              parsing, chunking, hybrid retrieval, reranking, citations
    agents/           intent classification, planner, tool orchestration
    prompts/          versioned prompt templates
    services/         cross-cutting application services
    observability/    structured logging, tracing, token/cost tracking
  tests/
frontend/
  src/                React + TypeScript research UI
evaluation/
  datasets/           curated Q&A ground truth
  scripts/            evaluation runners
  results/            recorded evaluation runs
docs/                 this file + methodology / decision docs (see below)
```

## No-lookahead-bias principle

Every pre-earnings snapshot stores only data that existed at `snapshot_timestamp`. Provider
records carry `retrieved_at` and, where known, the source's own timestamp, so any downstream
join can be audited for leakage. This is enforced in the data model, not just by convention —
details land with the schema in Phase 1.

## Planned documentation set

As each phase adds real substance, the following docs are created/expanded:

- `docs/data_sources.md` — providers evaluated and chosen, and why
- `docs/data_model.md` — table grain, keys, relationships, indexing, point-in-time semantics
- `docs/options_methodology.md` — implied move methodology, payoff formulas, Greeks assumptions
- `docs/earnings_methodology.md` — surprise, IV crush, implied-vs-realised methodology
- `docs/ai_architecture.md` — RAG design, agent orchestration, prompt versioning
- `docs/evaluation.md` — evaluation dataset design and measured results
- `docs/engineering_decisions.md` — key technical decisions and rejected alternatives
- `docs/limitations.md` — known gaps (historical options coverage, provider limits, etc.)
- `docs/interview_walkthrough.md` — talking points for discussing this project
- `docs/cv_entry.md` — CV bullets, finalized only after implementation is verified

## Build plan (phases)

Phase 0 — repository, GitHub, architecture skeleton *(this commit)*
Phase 1 — PostgreSQL schema, provider interfaces, first real earnings data
Phase 2 — market data + earnings expectation/outcome models
Phase 3 — options analytics engine
Phase 4 — historical earnings analytics
Phase 5 — document ingestion + RAG
Phase 6 — AI structured extraction
Phase 7 — agent tool orchestration
Phase 8 — frontend
Phase 9 — evaluation framework
Phase 10 — observability + deployment
Phase 11 — recruiter-facing polish

Each phase lands as its own commit(s) with passing tests, not as one final drop.
