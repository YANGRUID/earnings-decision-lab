# Earnings Decision Lab

AI-assisted earnings intelligence, options analytics, and historical event research.

> **Status: early development (Phase 0 — skeleton).** This README will be rewritten for
> recruiters once the core system is functional. See [docs/architecture.md](docs/architecture.md)
> for the current design and build plan.

## What this is

A personal research platform for a small set of semiconductor tickers (NVDA, AMD, MU, SNDK)
that combines:

- **Deterministic financial analytics** (Python) for earnings surprise, implied vs. realised
  move, IV crush, and options strategy payoffs.
- **Point-in-time data snapshots** so nothing in a pre-earnings record leaks information that
  wasn't actually available at that timestamp.
- **Evidence-grounded AI research** (RAG over SEC filings and earnings materials, tool-using
  agent orchestration) for questions that require reading and comparing text, not just
  calculating numbers.

LLMs are used for extraction, retrieval, summarization, and synthesis — never for arithmetic
that Python can compute exactly.

## Why this exists

This is a portfolio project built to demonstrate production-oriented AI engineering practice
(RAG, agentic tool use, typed APIs, testing, evaluation, observability, CI/CD, cloud
deployment) on a problem the author actually cares about: earnings-event research and options
analytics. It is not a generic chatbot wrapper and does not provide investment advice.

## Project layout

```
backend/    FastAPI service: providers, ingestion, analytics, RAG, agents, API
frontend/   React + TypeScript research UI
evaluation/ Curated Q&A dataset and scripts for measuring retrieval/answer quality
docs/       Architecture, methodology, and engineering-decision documentation
```

## Development status

See [docs/architecture.md](docs/architecture.md) for the phased build plan and current
progress. Nothing here is deployed or production-ready yet.

## Disclaimer

This project is for research and software-engineering purposes and does not provide
investment advice.

## License

MIT — see [LICENSE](LICENSE).
