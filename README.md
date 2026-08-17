# Earnings Decision Lab

**AI-assisted earnings intelligence, options analytics, and historical event research.**

A portfolio project built to demonstrate production-style AI engineering practice: hybrid RAG,
tool-calling agent orchestration, structured extraction, a typed FastAPI backend, a real
evaluation framework, and a Dockerized deployment-ready stack — applied to a problem the author
actually researches (earnings-event moves and options positioning for four semiconductor
tickers), not a generic demo.

> **Not investment advice.** Research and software-engineering portfolio project only. See
> [Disclaimer](#disclaimer).

## Recruiter quick view

| | |
|---|---|
| **What it does** | For NVDA, AMD, MU, SNDK: real historical earnings results and price reactions, real SEC filing search with citations, a deterministic options-strategy calculator, and an AI research assistant that plans and executes real tool calls (not a single LLM call pretending to be an agent) |
| **Stack** | Python 3.12 / FastAPI / SQLAlchemy 2.0 / Alembic / PostgreSQL + pgvector · React 18 + TypeScript / Vite · Docker Compose · GitHub Actions CI |
| **AI architecture** | Provider-agnostic LLM layer (DeepSeek / OpenAI / Anthropic / any OpenAI-compatible endpoint) · hybrid (vector + keyword, RRF-fused) retrieval over real SEC filings · explicit multi-stage agent (intent → plan → tool execution → verification), not a chatbot wrapper |
| **Real data, not fixtures in prod** | 150 real earnings events, 25k+ real daily price bars, 93 real SEC filings, 2,231 real filing chunks — sourced from SEC EDGAR, Tiingo, Alpha Vantage |
| **Measured, not claimed** | 243 automated tests (unit/API/provider/RAG/agent), a 51-item hand-verified evaluation dataset with real measured results (below), CI green on every push including a full Docker build-and-boot check |
| **Deployment status** | Docker-verified and deployment-ready (`docker compose up --build` runs the full real stack locally) — **not** deployed to a live public cloud; see [docs/deployment.md](docs/deployment.md) for the cost research and why |
| **Honesty as a design principle** | Every screen that has no real data shows an explicit "no data yet" state instead of a fake number. [docs/limitations.md](docs/limitations.md) tracks every known gap, phase by phase |

### Real, measured evaluation results

From the most recent run (`docs/evaluation.md` has full methodology, per-item results, and an
honest discussion of *why* retrieval scores what it does):

| Category | Metric | Result |
|---|---|---|
| Retrieval (18 hand-verified items) | Recall@5 | 35% |
| RAG answer (15 items, incl. 1 negative control) | Fact coverage | 70% |
| Agent orchestration (10 items) | Intent + tool-selection accuracy | 100% |
| Structured extraction (8 items) | Capex-guidance accuracy | 100% |

## Screenshots

| | |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) Dashboard | ![AI Research](docs/screenshots/ai_research.png) AI Research — real agent run: citations, tool call, verification, cost |
| ![Earnings Event](docs/screenshots/earnings_event.png) Earnings Event — real EPS/revenue/price reaction, honest empty state for unavailable options data | ![Options Lab](docs/screenshots/options_lab.png) Options Lab — deterministic payoff calculator |
| ![Company](docs/screenshots/company_amd.png) Company — real earnings history | ![Historical Replay](docs/screenshots/historical_replay.png) Historical Replay — honest empty state, not faked |

Full Data/Evaluation Status screen: [docs/screenshots/data_eval_status.png](docs/screenshots/data_eval_status.png).

## Architecture

```
backend/    FastAPI service: providers, ingestion, analytics, RAG, agents, API (see backend/README.md)
frontend/   React + TypeScript research UI (see frontend/README.md)
evaluation/ Hand-curated Q&A dataset and scripts that measure retrieval/RAG/agent/extraction quality
docs/       Architecture, methodology, and engineering-decision documentation (see below)
```

Five modules: **Earnings Expectations** (pre-earnings, point-in-time), **Earnings Outcomes**
(actuals, surprise, price reaction), **Options & Volatility Analytics** (deterministic payoff
engine, Black-Scholes Greeks, ATM-straddle implied move), **Historical Event Replay**
(rule-based strike selection — architecture complete, honestly empty pending a historical
options-chain data source), and **AI Research Assistant** (hybrid RAG + tool-calling agent over
the four modules above).

The no-lookahead-bias principle governs the whole data model: every pre-earnings snapshot
stores only what existed at that timestamp, and every externally-sourced record carries its
provider and retrieval time. Full design rationale — including deliberate scope boundaries and
what was evaluated and rejected — in [docs/engineering_decisions.md](docs/engineering_decisions.md).

## Documentation

| Doc | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System design, current status |
| [docs/data_model.md](docs/data_model.md) | Table grain, keys, indexing, point-in-time semantics |
| [docs/data_sources.md](docs/data_sources.md) | Providers evaluated, chosen, and why (incl. two rejected for blocking automated access) |
| [docs/options_methodology.md](docs/options_methodology.md) / [docs/earnings_methodology.md](docs/earnings_methodology.md) | Payoff formulas, Greeks assumptions, implied-move/IV-crush methodology |
| [docs/llm_providers.md](docs/llm_providers.md) | Provider-agnostic LLM layer |
| [docs/ai_architecture.md](docs/ai_architecture.md) | RAG pipeline + agent orchestration design |
| [docs/evaluation.md](docs/evaluation.md) | Evaluation methodology, real results, honest analysis |
| [docs/deployment.md](docs/deployment.md) | Docker architecture, real cost research for live hosting |
| [docs/engineering_decisions.md](docs/engineering_decisions.md) | Every major technical decision and why, phase by phase — including real bugs found and fixed along the way |
| [docs/limitations.md](docs/limitations.md) | Every known gap, stated plainly |
| [docs/interview_walkthrough.md](docs/interview_walkthrough.md) | Talking points for discussing this project |
| [PROJECT_AUDIT.md](PROJECT_AUDIT.md) | Independent verification of what actually works, run against the real repo |

## Local development

```bash
cp .env.example .env   # fill in real values — see docs/data_sources.md and docs/llm_providers.md
docker compose up --build
```

Runs the full stack: PostgreSQL + pgvector, one-shot Alembic migration, the FastAPI backend
(`http://localhost:8000`), and the frontend (`http://localhost:5173`). Backend and frontend can
also be run independently outside Docker — see `backend/README.md` and `frontend/README.md`.

## Disclaimer

This is a personal research and software-engineering portfolio project. It is not investment
advice, has no live users, and no claim is made about trading performance — see
[docs/limitations.md](docs/limitations.md) for a full, honest account of what is and isn't real.

## License

MIT — see [LICENSE](LICENSE).
