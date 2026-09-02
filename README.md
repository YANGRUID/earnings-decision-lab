# Earnings Decision Lab

A personal, forward-only research system for **earnings-event options decisions**. One engine
(V4) decides at **15:30 ET** on the last trading day before an earnings announcement, freezes the
evidence, observes an executable entry for **six standardized configurations**, and settles at
**15:30 ET on the first post-earnings trading day**. Every row is prospective and immutable.
Nothing is back-filled, nothing is simulated, and **no brokerage order is ever placed**.

> Not investment advice. A research instrument for one person, built to be honest about what it
> knows and what it does not.

## Contents

1. [What it is](#what-it-is) · 2. [Architecture](#architecture) · 3. [The V4 forward test](#the-v4-forward-test) ·
4. [Six configurations](#six-configurations) · 5. [Timing policy](#timing-policy) ·
6. [Market data (TWS)](#market-data-tws) · 7. [DeepSeek's role](#deepseeks-role) ·
8. [Research preparation](#research-preparation) · 9. [Product surfaces](#product-surfaces) ·
10. [Run locally](#run-locally) · 11. [Testing](#testing) · 12. [Documentation](#documentation) ·
13. [Safety](#safety) · 14. [Limitations](#limitations) · 15. [History](#history)

## What it is

- A **calendar-driven pipeline**: real earnings events are discovered, companies are resolved
  against SEC EDGAR, research (filings, price history, estimates, options snapshots, an AI thesis)
  is prepared automatically ahead of each event, and the V4 engine decides only when that research
  is ready.
- A **deterministic decision engine** with one narrow AI input: the DecisionView (direction,
  volatility view, move intent, confidence) comes from an explicitly configured DeepSeek model;
  everything after it — expected move, strike geometry, T+1 valuation, ranking, sizing — is
  deterministic Python with a version stamp.
- A **forward track record** per configuration, honest about sample size: below 30 settled
  observations a cohort shows `INSUFFICIENT SAMPLE` and no performance metric.

## Architecture

```
Earnings calendar (EarningsAPI → Finnhub fallback)          nightly 20:00 ET
  → company resolution (EDGAR)                              worker, on demand
  → business eligibility (≥ $10B market cap, US-listed, options chain)
  → research preparation queue                              nightly 21:00 ET + 13:00 ET catch-up + startup catch-up
  → research worker (filings, prices, estimates, options snapshot, AI thesis)
  → READY_FOR_V4_DECISION
  → V4 decision + entry observation                         15:30 ET, legal pre-earnings day (deadline guard 15:50 ET)
  → six configuration results, frozen candidates and legs
  → V4 settlement                                           15:30 ET, first post-earnings trading day
  → V4 Forward Track Record (six cohorts)
```

| Layer | Where |
|---|---|
| FastAPI backend, SQLAlchemy 2.0, Alembic, APScheduler | `backend/src` |
| Research worker (separate process, own TWS client id) | `backend/src/workers` |
| V4 engine (methodology, semantics, strike engine, T+1 grids, ranking, configurations) | `backend/src/analytics/decision/v4_*.py` |
| V4 forward evidence (append-only, DB trigger enforced) | `backend/src/models/v4_shadow.py` (`v4_shadow_*` tables) |
| V4 orchestration, cohorts, settlement, scheduler | `backend/src/services/v4_shadow_*.py` |
| Operations read model (pipeline states, readiness, staleness) | `backend/src/services/operations.py` |
| React + Vite frontend | `frontend/src` |
| PostgreSQL + pgvector, Docker Compose | `docker-compose.yml` |

The internal module prefix `v4_shadow_*` is historical (V4 began as a shadow cohort next to the
retired V3 engine). In the product these are the **V4 Forward Test** surfaces.

## The V4 forward test

- **Prospective only.** A V4 record exists only because the engine observed a real market at the
  legal window. Empty states are genuinely empty.
- **Immutable.** Every `v4_shadow_*` table rejects updates at the database level; a decision,
  candidate, configuration result, observation or settlement can be superseded by a new row,
  never edited.
- **Never an order.** No order API, no order model, no brokerage write path exists in the
  codebase. "Entry observed" means an executable quote at the required side was recorded.
- **Delayed stays delayed.** Every quote carries its `market_data_quality`; delayed data is
  labelled delayed everywhere it is shown.

First natural sample: **AVGO**, decided 2026-09-02 15:32 ET, six configurations observed at
entry, settling prospectively 2026-09-03 15:30 ET.

## Six configurations

| Key | Capital | Risk cap | Max risk | Liquidity floor | Families |
|---|---|---|---|---|---|
| `v4_2k_conservative` | $2,000 | 15% | $300 | 0.80 | no single-leg longs |
| `v4_2k_moderate` | $2,000 | 30% | $600 | 0.40 | all generated |
| `v4_2k_aggressive` | $2,000 | 50% | $1,000 | none | all generated |
| `v4_10k_conservative` | $10,000 | 15% | $1,500 | 0.80 | no single-leg longs |
| `v4_10k_moderate` | $10,000 | 30% | $3,000 | 0.40 | all generated |
| `v4_10k_aggressive` | $10,000 | 50% | $5,000 | none | all generated |

All six evaluate the same frozen evidence and may independently produce `RANKED` or
`NO_ACTION`. `NO_ACTION` is evidence, never relaxed away.

## Timing policy

`v4-1530-entry-1530-t1-settlement-v2` (`backend/src/analytics/decision_timing_policy.py`):

| Announcement | Decision / entry | Settlement |
|---|---|---|
| After market close (AMC) | D0 15:30 ET | D+1 15:30 ET |
| Before market open (BMO) | D−1 15:30 ET | D0 15:30 ET |

Settlement is bounded to ±5 minutes around 15:30 ET; a missed window is recorded as
`SETTLEMENT_WINDOW_MISSED`, never settled late. The decision pass has a 15:50 ET deadline guard
(`DEADLINE_SKIPPED`). Policy v1 (15:55 ET settlement) was replaced prospectively on 2026-09-02:
rows frozen under v1 keep their stamp; every settlement records the version it ran under.

## Market data (TWS)

Interactive Brokers TWS over the socket API (`IBKR_PROVIDER=tws`): one long-lived connection per
process (backend client id, research worker client id), bounded timeouts, typed errors, a
per-run TWS request budget, and a shared in-process lock so the decision and settlement sweeps
never run concurrently. Delayed entitlements are reported as delayed. See
[docs/ibkr_architecture.md](docs/ibkr_architecture.md).

## DeepSeek's role

Exactly one AI input reaches the decision: the **DecisionView**, generated by
`V4_DECISION_VIEW_MODEL` (production: `deepseek-v4-pro`, thinking enabled, reasoning effort
high) with full provenance frozen on every decision (configured and returned model, thinking,
effort, tokens, latency, prompt and schema versions). There is no fallback model: a missing or
invalid configuration produces a recorded failure, not a substitute view. The same provider
writes the research **AI thesis** with the general model. DeepSeek never ranks, sizes, prices or
places anything.

## Research preparation

Research is prepared **automatically**:

- nightly at 21:00 ET (misfire grace 6 h, coalesced), a same-day catch-up at 13:00 ET, and a
  one-shot catch-up 90 s after every backend start;
- the queue re-enqueues any company whose research is not V4-ready (a company record plus an AI
  thesis younger than 7 days);
- the worker resolves unknown symbols against EDGAR; a symbol EDGAR cannot resolve is recorded as
  `COMPANY_RESOLUTION_FAILED`, never fabricated.

Live Operations shows readiness KPIs (upcoming → eligible → resolved → queued/running/ready →
AI thesis ready → V4 decision ready) and per-job freshness (`ON TIME` / `STALE` / `MISSED RUN`).

## Product surfaces

Dashboard · Company Search · Company workspace (Overview, Earnings Setup, Research, Market
View, V4 Decision, Candidates, Forward Outcome) · AI Research · V4 Decision Lab · Candidate
Explorer · V4 Forward Track Record · Live Operations · Settings (Data Providers, AI Provider,
IBKR / TWS, API Usage) · System Status.

## Run locally

```bash
cp .env.example .env            # fill in your own keys; never commit .env
docker compose up -d db migrate backend research-worker frontend
open http://localhost:5173
```

The production compose file runs the backend on `:8000`, the frontend on `:5173`, PostgreSQL on
`:5433`. Rebuild after code changes with `docker compose build backend research-worker migrate
frontend` and `docker compose up -d --no-deps backend research-worker frontend`. Do not restart
the backend inside the 15:30 ET windows.

## Testing

**Backend**: `cd backend && .venv/bin/python -m pytest -q` — the suite rebinds every session
factory to the disposable test database (`:5434`) and runs `alembic upgrade head` there; it cannot
reach the application database or open a live TWS socket. `ruff` and `mypy` clean.

**Frontend**: `npx tsc -b`, `npx eslint . --max-warnings 0`, `npm run build`, and Playwright
(`npx playwright test`) with route-mocked fixtures — including deterministic SPA navigation tests
(A→B→C→D without refresh, refresh, back/forward) and a slow-endpoint test proving abandoned
requests are aborted.

## Documentation

| Doc | Covers |
|---|---|
| [docs/v4_architecture.md](docs/v4_architecture.md) | One event → one evidence freeze → six results; modules and tables |
| [docs/v4_methodology.md](docs/v4_methodology.md) | Objective, strategy semantics, capital terminology, why V4 exists |
| [docs/v4_4b_candidate_ranking_methodology.md](docs/v4_4b_candidate_ranking_methodology.md) | The frozen T+1 executable ranking |
| [docs/v4_forward_testing.md](docs/v4_forward_testing.md) | Evidence rules, timing policy, windows, model provenance, activation |
| [docs/ibkr_architecture.md](docs/ibkr_architecture.md) / [docs/ibkr_integration.md](docs/ibkr_integration.md) / [docs/ibkr_gateway_runtime.md](docs/ibkr_gateway_runtime.md) | TWS integration and runtime |
| [docs/ai_architecture.md](docs/ai_architecture.md) / [docs/llm_providers.md](docs/llm_providers.md) | RAG pipeline, agent, provider layer |
| [docs/options_methodology.md](docs/options_methodology.md) / [docs/earnings_methodology.md](docs/earnings_methodology.md) | Shared payoff, pricing, implied-move and earnings analytics |
| [docs/data_model.md](docs/data_model.md) / [docs/data_sources.md](docs/data_sources.md) | Tables and providers |
| [docs/evaluation.md](docs/evaluation.md) | RAG / agent evaluation methodology and results |
| [docs/deployment.md](docs/deployment.md) | Docker architecture |
| [docs/engineering_decisions.md](docs/engineering_decisions.md) | Decisions by phase (historical record) |
| [docs/limitations.md](docs/limitations.md) | Known gaps |

## Safety

No order placement, no order API, no brokerage write path. Delayed data stays labelled delayed.
Tests cannot reach production. Evidence tables reject updates at the database level. The V4
forward test flag is never a code default. Credentials live only in `.env`.

## Limitations

- One natural sample so far; no performance claim is made or implied.
- Delayed market data; live/paper account status is not knowable over TWS and is shown as unknown.
- No capital ledger: metrics are per-decision and standardized, never portfolio drawdown or Sharpe.
- Aggressive differs from Moderate only through risk cap and liquidity floor (documented open
  methodology question).

## History

The V3 decision engine, its 15:55 ET benchmark pipeline, track record, AI Decision Journal,
Cross-Company Replay, Strategy Lab and their evidence tables were retired on 2026-09-02 (the
V4-only reset). The pre-reset state is preserved on the `archive/pre-v4-only-reset` branch.

## Disclaimer

This is a personal research tool. Nothing here is investment advice, a recommendation, or an
offer to trade. Options involve substantial risk.

## License

See [LICENSE](LICENSE).
