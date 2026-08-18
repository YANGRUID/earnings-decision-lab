# Earnings Decision Lab

**An on-demand earnings research and options decision system.** Search a stock, and the system
prepares its earnings history, filings, expectations, price data, and option-chain data, then
combines deterministic financial analytics with grounded AI research.

> **Not investment advice.** A personal research tool, not a trading system. See
> [Disclaimer](#disclaimer).

## Why this exists

Before an earnings release, I want to know: what happened last time, how did the stock react,
what is the market currently pricing in, and what has management actually said in its own
filings — not a summary someone else wrote, but the real text, cited. Most of that requires
pulling together data from several places (SEC filings, price history, options pricing) and
reading dense documents quickly. This system does the mechanical parts (fetching, storing,
computing) deterministically in Python, and uses an LLM only for the parts that genuinely need
language understanding — reading filing text, answering questions, comparing guidance language
across quarters — never for arithmetic.

## How it works

1. **Search a stock** by ticker.
2. **Prepare or refresh research** — earnings history, filings, price data, analyst estimates,
   and options data are ingested for that ticker on demand, with progress shown while it runs.
3. **Analyze the next earnings event** — consensus expectations, implied move, and ATM IV for
   the next, unreported report.
4. **Inspect options market pricing** — a real, live option chain: strikes, bid/ask, IV, Greeks,
   volume/open interest where available.
5. **Compare strategy candidates** — deterministic, ranked options strategies built from that
   real chain, with exact breakeven, max profit, and max loss.
6. **Check historical move compatibility** — how the current implied move compares to what
   actually happened in the company's own past earnings reports.
7. **Read the grounded AI Earnings Thesis** — a cited synthesis of filings, historical pattern,
   and guidance trend for that company.
8. **Review IBKR exposure** — real, read-only positions from your own Interactive Brokers
   account that match the researched ticker, if any.
9. **Make your own decision** — every number here is computed from real data; nothing in this
   system is a recommendation or a probability of profit.

## Current capabilities

**On-demand research**
- Search any supported US-listed ticker; symbol resolution against a real reference dataset
- On-demand ingestion — a ticker's earnings history, filings, price data, and options data are
  fetched and stored only once that ticker is actually requested
- Freshness-aware refresh — re-running research only re-fetches what's stale, not everything
- Real preparation progress and status for every run (e.g. `completed_with_warnings`, with real
  start/end timestamps), not a silent black box

**Earnings**
- Real historical earnings events — EPS, report date, next-day and five-day price reaction —
  sourced from SEC EDGAR and Tiingo/Alpha Vantage
- The next, unreported earnings event tracked separately from historical events, so expectation
  and outcome are never mixed
- Analyst consensus (EPS/revenue estimates, analyst count, revision trend) for the next report
- Historical move statistics (average, median, largest move) computed from a company's own
  reported history

**Options**
- Real option-chain data from the user's own Interactive Brokers account — strikes, bid/ask,
  last, implied volatility, delta, volume/open interest where available
- Market-data quality shown per quote (e.g. live vs. frozen), never presented as more certain
  than it is
- Implied move and ATM IV computed from the real chain via an ATM-straddle method
- Put/call open-interest ratio as a real sentiment signal

**Strategy Lab**
- Real option-chain-based candidate generation across common strategies (spreads, straddles,
  strangles, iron condor, and more)
- Deterministic breakeven, max profit, and max loss for every candidate, computed from real
  strikes and premiums — never an LLM estimate
- Deterministic, explainable ranking by payoff at the market's own implied move
- A historical move compatibility check per candidate — see [Limitations](#limitations) for why
  this is not a backtest
- Advanced: a manual payoff calculator for arbitrary strikes/premiums, as a fallback outside the
  generated candidates

**AI**
- Retrieval-augmented generation over real SEC filing text (hybrid vector + keyword search)
- Cited answers to natural-language questions about a company's filings, earnings, and guidance
- A grounded, structured Earnings Thesis per company — business context, historical earnings
  pattern, guidance trend, key risks — synthesized only from data already shown elsewhere in the
  workspace
- Explicit tool orchestration: every AI Research answer shows which tools were called and how it
  was verified, not just the final text
- Guidance comparison across quarters as a structured, deterministic diff

**Portfolio**
- Real, read-only positions from the user's own Interactive Brokers account, matched to the
  researched ticker
- Never a market-quote source; never places, modifies, or cancels an order

**System**
- Python 3.12 / FastAPI / SQLAlchemy 2.0 / Alembic / PostgreSQL + pgvector
- React 18 + TypeScript / Vite frontend
- Docker Compose for the full stack; GitHub Actions CI (frontend, backend, and Docker
  build-and-boot jobs) on every push
- Provider abstraction for market data, filings, and options, so a provider can be swapped
  without touching calling code

## Supported tickers and data

NVDA, AMD, MU, and SNDK were the original companies researched while building this system, but
they are not a hard-coded limit. Search prepares research for any supported US-listed ticker on
demand — detailed data (earnings history, filings, price bars, options snapshots) is ingested and
persisted only once a ticker is actually requested, rather than preloading thousands of companies
nobody has asked about. Provider coverage (SEC EDGAR, Tiingo, Alpha Vantage, IBKR) can vary by
ticker: a newly requested company may come back with partial data, or a preparation run may
complete with warnings, if a provider has limited or no data for it.

As of the most recent local run (see the System Status screenshot below, and the live
`GET /api/v1/system-status` endpoint): 5 companies researched, 201 earnings events, 29,985 daily
price bars, 127 SEC filings, 2,607 searchable filing chunks. These are live counts for one local
deployment, not a fixed catalog — they change every time a ticker is researched, so treat them as
an example snapshot rather than a permanent number.

## Screenshots

| | |
|---|---|
| ![Home](docs/screenshots/home.png) **Home** — search-first, recently researched companies | ![Upcoming Earnings](docs/screenshots/upcoming_earnings.png) **Upcoming Earnings** — consensus, implied move, ATM IV, historical comparison |
| ![Strategy Lab](docs/screenshots/strategy_lab.png) **Strategy Lab** — ranked candidates from a real option chain, with breakeven and historical compatibility | ![Option Chain](docs/screenshots/option_chain.png) **Option Chain** — real strikes, bid/ask, delta, IV, OI, market-data quality |
| ![AI Earnings Thesis](docs/screenshots/earnings_thesis.png) **AI Earnings Thesis** — grounded, cited synthesis | ![Historical Events](docs/screenshots/history.png) **Historical Events** — real past earnings moves |

Additional screens: [AI Research](docs/screenshots/ai_research.png) (grounded Q&A with tool
trace) · [System Status](docs/screenshots/system_status.png) (live data coverage and freshness) ·
[Cross-Company Replay](docs/screenshots/cross_company_replay.png).

## Architecture

```
backend/    FastAPI service: providers, research orchestration, analytics, RAG, agents, API
frontend/   React + TypeScript research workspace
evaluation/ Hand-curated Q&A dataset and scripts that measure retrieval/RAG/agent/extraction quality
docs/       Architecture, methodology, and engineering-decision documentation (see below)
```

```
User research request
  -> Research Preparation Orchestrator (symbol resolution, freshness/cache check)
  -> Providers (SEC EDGAR, Tiingo, Alpha Vantage, IBKR)
  -> PostgreSQL + pgvector
  -> Deterministic analytics (earnings, options, strategy ranking, historical move compatibility)
  -> AI research / grounded Earnings Thesis (RAG + tool orchestration)
  -> React research workspace
```

The no-lookahead-bias principle governs the whole data model: every pre-earnings snapshot stores
only what existed at that timestamp, and every externally-sourced record carries its provider and
retrieval time. Full design rationale — including deliberate scope boundaries and what was
evaluated and rejected — in
[docs/engineering_decisions.md](docs/engineering_decisions.md).

## Interactive Brokers integration

Real option-chain data and portfolio positions come from the user's own Interactive Brokers
account via the official, locally-run **Client Portal Gateway** — read-only, no order placement,
modification, or cancellation anywhere in the integration. The Gateway is deliberately
**local-first**: it runs and is authenticated on the user's own machine, and this project never
sees an IBKR username, password, or 2FA code. Cloud/Azure synchronization for IBKR data has not
been implemented — if this system is ever deployed off a local machine, the Gateway either keeps
running locally and forwards already-collected snapshots, or a different, officially supported
IBKR cloud authentication model would need to be adopted first. That decision is explicitly
deferred, not solved by what exists today. Full architecture and real, live-captured
request/response examples in [docs/ibkr_integration.md](docs/ibkr_integration.md).

## Deterministic analytics vs. AI

A core design principle of this system: **all financial arithmetic is deterministic Python, never
an LLM guess.** Breakeven, max profit/loss, net premium, implied move, ATM IV, historical move
statistics, and strategy ranking scores are all computed by plain code with unit tests. The LLM is
used only where the task genuinely requires language understanding — interpreting filing text,
synthesizing evidence into an Earnings Thesis, answering a natural-language question, comparing
guidance wording across quarters. Every AI-generated answer is grounded in data computed or
retrieved by the deterministic layer, cited back to its source, and never the source of a number
that appears elsewhere in the app.

## Measured evaluation

This system's AI components are evaluated against a hand-curated, hand-verified dataset, not
graded on impression
([docs/evaluation.md](docs/evaluation.md) has full methodology, per-item results, and an honest
discussion of *why* retrieval scores what it does):

| Category | Metric | Result |
|---|---|---|
| Retrieval (18 hand-verified items) | Recall@5 | 35% |
| RAG answer (15 items, incl. 1 negative control) | Fact coverage | 70% |
| Agent orchestration (10 items) | Intent + tool-selection accuracy | 100% |
| Structured extraction (8 items) | Capex-guidance accuracy | 100% |

## Testing and quality

Verified against the current repository: 465/465 backend tests passing on a clean database (a
handful can show as failing against a long-running, actively-used shared dev database once real
research data exists in it — expected, not a regression), `ruff` clean, `mypy` clean, frontend
`tsc` typecheck clean, frontend `eslint` clean, frontend production build clean, and a full Docker
Compose stack (`db` → `migrate` → `backend` → `frontend`) building and booting successfully.
GitHub Actions runs the frontend, backend, and Docker jobs on every push to `main`, all green as
of the latest commit.

## Documentation

| Doc | Covers |
|---|---|
| [docs/architecture.md](docs/architecture.md) | System design as of Phase 11 — schema, providers, and module status at that point; superseded on research orchestration, Strategy Lab, and AI Thesis by this README and the phases below |
| [docs/data_model.md](docs/data_model.md) | Table grain, keys, indexing, point-in-time semantics |
| [docs/data_sources.md](docs/data_sources.md) | Providers evaluated, chosen, and why (incl. two rejected for blocking automated access) |
| [docs/options_methodology.md](docs/options_methodology.md) / [docs/earnings_methodology.md](docs/earnings_methodology.md) | Payoff formulas, Greeks assumptions, implied-move methodology |
| [docs/llm_providers.md](docs/llm_providers.md) | Provider-agnostic LLM layer |
| [docs/ai_architecture.md](docs/ai_architecture.md) | RAG pipeline + agent orchestration design |
| [docs/ibkr_integration.md](docs/ibkr_integration.md) | Interactive Brokers integration architecture, auth flow, real request/response examples |
| [docs/evaluation.md](docs/evaluation.md) | Evaluation methodology, real results, honest analysis |
| [docs/deployment.md](docs/deployment.md) | Docker architecture, real cost research for hosting |
| [docs/engineering_decisions.md](docs/engineering_decisions.md) | Every major technical decision and why, phase by phase (Phases 1–13), including real bugs found and fixed along the way |
| [docs/limitations.md](docs/limitations.md) | Known gaps for Phases 1–10 in detail; see this README's own [Limitations](#limitations) section for what's specific to later phases |
| [SYSTEM_AUDIT.md](SYSTEM_AUDIT.md) | An independent verification pass against the real system, dated to Phase 11 |

## Local setup

```bash
cp .env.example .env   # fill in real values — see docs/data_sources.md and docs/llm_providers.md
docker compose up --build
```

Runs the full stack: PostgreSQL + pgvector, a one-shot Alembic migration, the FastAPI backend
(`http://localhost:8000`), and the frontend (`http://localhost:5173`). Backend and frontend can
also be run independently outside Docker — see `backend/README.md` and `frontend/README.md`.

Everything works without a running IBKR Client Portal Gateway except real option-chain data,
IBKR-sourced implied move/ATM IV, and Portfolio/My Exposure — those show an explicit
not-available state instead of a number. See
[docs/ibkr_integration.md](docs/ibkr_integration.md) for how to run and authenticate the Gateway
locally.

## Limitations

- The IBKR Client Portal Gateway requires local authentication on the user's own machine; there
  is no cloud-hosted IBKR connection today, and Azure/cloud synchronization for IBKR data has not
  been built (see [Interactive Brokers integration](#interactive-brokers-integration)).
- Strategy Lab's ranking is a deterministic heuristic based on payoff at the market's own implied
  move — not a profit predictor and not a recommendation.
- Historical move compatibility compares the current implied move against real past earnings-day
  moves; it is not a backtest, because complete historical point-in-time options-chain data does
  not exist for past earnings events.
- Data coverage and freshness depend on the underlying providers (SEC EDGAR, Tiingo, Alpha
  Vantage, IBKR) — rate limits, provider outages, or a ticker with limited coverage can produce
  partial results or a preparation run that completes with warnings.
- AI-generated research (the Earnings Thesis, AI Research answers) is grounded in retrieved
  evidence but is only as complete as that evidence — a newly researched company with sparse
  filing history produces a correspondingly limited answer.

Full, phase-by-phase list for Phases 1–10 in [docs/limitations.md](docs/limitations.md).

## Disclaimer

This is a personal research tool. It is not investment advice, has no live users, and no claim is
made about trading performance — see [docs/limitations.md](docs/limitations.md) and the
[Limitations](#limitations) section above for a full, honest account of what is and isn't real.

## License

MIT — see [LICENSE](LICENSE).
