# Architecture Review — Phase 4: AI Earnings Analyst Simulator with Verified Forward-Test Performance

Status: **review only — no production code written yet.** This document exists to satisfy the
explicit instruction not to start major implementation before the existing system is
understood and a design is committed to in writing. It is organized around the 11 questions
the phase brief asked for, plus an up-front summary of the decisions that fall out of them.

Branch: `feature/ai-earnings-forward-test`, based on local `main` at the tip of Options
Decision Engine V3 (commits `ba6d8de`, `cd6224e`, `a5a1a90` — none of it pushed to `origin`
yet).

---

## 0. Executive summary — the decisions this review makes

Read the full document for the reasoning; this is the short version so the shape of Phase 4
is clear before the detail.

1. **Two earnings-date mechanisms will coexist, not merge.** The existing per-ticker,
   Alpha-Vantage-sourced `EarningsEstimateSnapshot` stays exactly as-is and keeps powering the
   existing on-demand "research a ticker" flow. A **new** `earnings_calendar_event` table,
   populated only from Finnhub, powers the new automated forward-test flow. They cross-link
   (a scheduler-generated benchmark decision writes a real `EarningsEstimateSnapshot` row with
   a new `date_source=FINNHUB` so it flows through the *existing, already-tested*
   `generate_decision()` pipeline unmodified) but neither table is deleted or repointed.

2. **`earnings_event` is not reused for the Finnhub calendar.** Its grain
   `(company_id, fiscal_year, fiscal_quarter)` and its SEC-XBRL provenance make it a
   *retrospective* table (see §1.2). The new calendar is a **new** table with its own grain and
   its own upsert key.

3. **The immutable decision is a new table, not a retrofit of `ai_decision_version`.** That
   table already carries the right *spirit* (append-only, settlement fields bolted on once) but
   is scoped to the user-driven manual-decision feature, mixes generation payload with
   settlement payload in one wide table, and — critically — **never computes real option P&L**
   by explicit, permanent, documented design (`strategy_pnl` is always `None`). Phase 4 needs
   real P&L, so it needs a real entry/exit option-price capture pipeline that does not exist
   anywhere in this codebase today. New table: `decision_snapshot`.

4. **There is no working scheduler today.** `apscheduler>=3.10` is a declared dependency and is
   never imported anywhere. Every existing "scheduled" script (`ingestion/collect_options_
   snapshots.py`, `ingestion/capture_close_snapshot.py`) documents itself as "run via cron" but
   no cron, GitHub Actions schedule, or in-process scheduler actually exists in this repo — they
   are run by hand or by an undocumented host-level job outside version control. Phase 4 is the
   first feature that actually requires guaranteed, precisely-timed execution (calendar sync,
   eligibility scan, 15:55 ET entry, T+1 15:55 ET exit), so it is the first feature that must
   actually wire up a scheduler. Recommendation: `AsyncIOScheduler` from the already-installed
   `apscheduler`, started from FastAPI's existing `lifespan()` hook in `api/main.py`, inside the
   `backend` container (the only component docker-compose guarantees is always running).

5. **No existing provider interface supports a cross-symbol calendar scan.** `EarningsData
   Provider.get_earnings_calendar(ticker)` and `EarningsEstimatesProvider.get_next_earnings_
   date(ticker)` are both single-ticker methods. A Finnhub adapter needs new surface area: a
   new provider class with a `get_earnings_calendar(from_date, to_date) -> list[...]` shape that
   no existing ABC declares. This is genuinely new, not a gap-fill.

6. **A real, honest, and unavoidable operational limitation carries forward from Phase 13/14:**
   IBKR is a local Gateway that requires periodic manual re-authentication (confirmed live,
   twice, during Phase 14). Automated 15:55 ET entry/exit capture **will sometimes fail**
   because the Gateway session has expired and nobody was there to re-log-in. The design below
   treats a missed capture as an honest `SKIPPED`/`FAILED` state, never a fabricated price —
   this is a real constraint on what "verified forward-test performance" can promise, and it
   needs to be visible in the product, not hidden.

7. **One open question this review cannot resolve alone — flagged for you, not guessed at:**
   the phase brief's entry-timing example (`Earnings: Monday AMC → Exit: Tuesday 15:55 ET`)
   only covers the after-market-close case. A **before-market-open (BMO)** event reporting on
   day *D* has already happened by the time *D*'s 15:55 ET arrives — entry must be captured on
   day *D-1* instead, or the decision is generated with look-ahead bias (the exact thing this
   phase is designed to prevent). §4.3 lays out the two sessions' correct entry days precisely;
   I did not silently pick one without surfacing it, since getting this wrong would violate the
   phase's own "No look-ahead bias" constraint.

---

## 1. Current database schema

24 SQLAlchemy model files, 21 Alembic migrations, PostgreSQL + pgvector. Full grain-by-grain
detail (every column, every FK, every unique constraint) was captured file-by-file during this
review; the tables most relevant to Phase 4 are summarized below. (Every other table —
`document_chunk`, `filing`, `ai_extraction`, `ai_research_query`, `ai_thesis_version`,
`portfolio_position_snapshot`, `provider_credential`, `provider_health_event`, `provider_usage_
event`, `app_provider_settings` — is unrelated to this phase and untouched by it.)

### 1.1 `company` — the master ticker table

```
id (PK), ticker (unique, indexed), name, cik (nullable, unique),
sector (nullable), exchange (nullable), is_active (bool, default True)
```

**No `market_cap`. No `logo_url`. No explicit "listed country" field** (`exchange` is a
free-text code, not a normalized country/venue). `cik` is nullable — a company does not
strictly need SEC coverage to exist as a row, which matters because Finnhub will discover
companies this system may never have ingested a filing for.

### 1.2 `earnings_event` — the *existing* earnings-event hub, and why it's the wrong table for Phase 4

```
id (PK), company_id (FK→company),
fiscal_year (int), fiscal_quarter (int),
period_end_date (nullable date, from XBRL),
earnings_date (nullable date, indexed),
announcement_time (enum AnnouncementTime: BEFORE_MARKET | AFTER_MARKET | UNKNOWN, default UNKNOWN),
date_confirmed (bool, default False)
UNIQUE (company_id, fiscal_year, fiscal_quarter)
```

Everything else in the schema — `earnings_result`, `price_reaction`, `earnings_expectation_
snapshot`, `strategy_replay`, and (optionally) `ai_decision_version.earnings_event_id` — hangs
off this table by FK. It is populated by SEC-XBRL backfill scripts (`ingestion/backfill_
earnings_dates.py`, `ingestion/earnings_date_backfill.py`) and is fundamentally
**retrospective**: its unique key requires knowing `fiscal_year`/`fiscal_quarter`, which for a
brand-new Finnhub-discovered company reporting next week may not be resolvable without XBRL
data that won't exist until *after* the filing lands. Forcing the Finnhub forward calendar
into this table would mean either inventing fiscal-quarter numbers before they're confirmed
(a real correctness risk — this project has explicitly avoided guessing fiscal periods
elsewhere, see `EarningsEstimatePeriod`'s docstring on why it's keyed by period-end-date
instead) or leaving them null and defeating the unique constraint. **Decision: new table.**

`announcement_time`'s `AnnouncementTime` enum is BMO/AMC only — no "during market hours"
value. Since Phase 4 explicitly wants BMO/AMC/DMH, and this enum is used in exactly one place
today, the plan is to **add `DURING_MARKET` to the existing enum** rather than create a
parallel one, and reuse it on the new calendar table too — one enum, one meaning, everywhere a
session is recorded.

### 1.3 `earnings_estimate_snapshot` — the *existing* "next earnings date" mechanism

```
id (PK), company_id (FK→company),
fiscal_period_end_date (date, indexed), horizon (str),
snapshot_timestamp (datetime, indexed),
eps_estimate_average/high/low, eps_estimate_analyst_count,
eps_estimate_revision_up_30d/down_30d, eps_revision_direction (enum),
revenue_estimate_average/high/low, revenue_estimate_analyst_count, revenue_revision_direction (enum),
estimated_report_date (nullable date),
date_source (enum UpcomingEarningsDateSource: ALPHA_VANTAGE | MANUAL | ESTIMATED | UNKNOWN, default ALPHA_VANTAGE),
source_provider (str), retrieved_at (datetime)
UNIQUE (company_id, fiscal_period_end_date, snapshot_timestamp)
```

This is what `generate_decision()` reads today (via `get_latest_earnings_estimate`) to decide
"when is this company's next earnings." Append-only — a new snapshot is a new row, never an
edit. `date_source` already has an extension point: `ESTIMATED`/`UNKNOWN` are declared but
never written anywhere in the codebase today (confirmed by grep), so adding `FINNHUB` here is
a one-line enum change with a real, clean precedent.

**Decision: when the new scheduler generates a benchmark decision for a Finnhub calendar
event, it writes one of these rows too** (`date_source=FINNHUB`, all consensus fields from
Finnhub's estimate if provided, else null), specifically so `generate_decision()` — which
already correctly handles `estimated_report_date`/`date_source`/manual overrides/consensus
staleness — runs completely unmodified. This avoids forking the decision-generation pipeline
into a "Finnhub path" and an "Alpha Vantage path" that could silently drift apart.

### 1.4 `earnings_expectation_snapshot` — the only existing `market_cap` column

```
id (PK), earnings_event_id (FK→earnings_event, indexed),
snapshot_timestamp (datetime, indexed),
consensus_eps, consensus_revenue, previous_guidance,
analyst_revision_direction (enum),
stock_price, market_cap, sector,
atm_implied_volatility, implied_move_pct, implied_move_absolute, iv_percentile,
near_term_iv, next_term_iv, term_structure_slope,
put_call_open_interest_ratio, put_call_volume_ratio,
recent_return_5d, recent_return_20d, sector_return, market_return,
source_provider, retrieved_at
UNIQUE (earnings_event_id, snapshot_timestamp, source_provider)
```

This is the *only* `market_cap` field anywhere in the schema — and it's keyed to
`earnings_event_id`, which per §1.2 doesn't exist yet for a not-yet-reported Finnhub event.
Whether this table is actually populated by anything today was outside this review's read set
(the model exists and is migrated; nothing in the flows reviewed above wrote to it) — **do not
assume it's live data**; verify with a real query before reusing it as an eligibility-filter
data source. The eligibility filter (§7, Phase 5) should treat market cap as its own concern
sourced fresh from Finnhub's profile endpoint, not depend on this table being populated.

### 1.5 `ai_decision_version` — the existing decision journal, and why it's not the immutable snapshot

Full column list (confirmed exhaustively):

```
id, company_id (FK),
direction (enum), volatility_view (enum),
confidence_score (int), confidence_components (JSON),
rationale/bull_case/bear_case/key_catalysts/key_risks/disclaimer (Text),
citations (JSON),
decision_source (enum: AI | MANUAL_OVERRIDE),
risk_preference (str), risk_profile (str, nullable),
recommended_strategy_category/legs/analysis/score/score_components/why/risks (nullable JSON/scalar),
recommended_strategy_why_expiration/why_strikes/why_risk_profile/why_not_alternative (nullable JSON),
alternative_strategies (nullable JSON),
expiration, underlying_price, implied_move_pct,
trade_budget, risk_cap, risk_cap_is_percent,
recommended_quantity, recommended_capital_at_risk, budget_infeasible_minimum,
no_market_data_reason,
provider, model,
earnings_estimate_snapshot_id (FK, nullable), volatility_snapshot_id (FK, nullable),
status (enum: OPEN | SETTLED | VOID, default OPEN),
is_final (bool, default False),
earnings_event_id (FK, nullable),
actual_next_day_move_pct, actual_five_day_move_pct,
direction_correct, actual_move_exceeded_implied, breakeven_met (nullable bool),
strategy_pnl (nullable Decimal — ALWAYS null, see below),
strategy_pnl_available (bool, ALWAYS False),
settled_at (nullable datetime)
```

Its own module docstring is explicit and permanent: *"Actual options P&L is NEVER computed.
This project does not capture real point-in-time entry/exit option premiums for expired
contracts... strategy_pnl stays null and strategy_pnl_available stays False for every
settlement this function ever produces. Do not add strategy P&L computation here without first
building that real data pipeline."* Phase 4 **is** that real data pipeline — but per Critical
Constraint #4 in the phase brief ("Separate: AI Benchmark Portfolio from User Manual
Decisions"), it does not belong bolted onto this table. It belongs on a new one.

Also worth noting for the "immutable" requirement: `ai_decision_version`'s append-only-ness is
a **service-layer convention**, not a DB-level guarantee — no trigger, no `updated_at`-freeze,
no read-only role. `decision_snapshot` should follow the same convention (a small, explicit set
of writer functions, each callable at most once per row) and be honest that this, too, is a
convention enforced by discipline in the service layer, not a hard database guarantee — adding
a hard guarantee (e.g., a Postgres `BEFORE UPDATE` trigger rejecting changes to frozen columns)
is a reasonable stretch goal, not something to skip the review over.

### 1.6 Everything else touched by reuse

- `options_snapshot` — real per-contract quote at a point in time. Reused as-is for entry/exit
  capture: a real capture just runs `provider.get_option_chain(...)` and can, if wanted, persist
  the raw quotes into this exact table (tagging `purpose` appropriately) in addition to
  recording the specific leg prices actually used inside `decision_snapshot`.
- `volatility_snapshot`, `price_bar`, `price_reaction`, `earnings_result` — all reused unchanged
  as inputs. `price_reaction.next_day_close` is a strong candidate for the settlement
  engine's V1 underlying exit price (see §9 gap: it's the stock's close, not an option price —
  still useful context, not a substitute for real option exit pricing).

---

## 2. Existing Options Decision Engine V3 architecture

The V3 pipeline (this session's prior work, now on `main`) is a pure-computation core with a
thin persistence shell, and Phase 4 reuses the core wholesale:

```
generate_decision(db, llm, embedder, company, *, direction_override=, volatility_view_override=,
                   trade_budget=, risk_cap=, risk_cap_is_percent=, risk_profile=,
                   manual_expiration=) -> DecisionResult
```

in `services/decision_engine.py`. It:

1. Gets-or-generates the AI Earnings Thesis (LLM + RAG over SEC filings).
2. Resolves the option market (real chain via `resolve_best_actionable_option_market`, or a
   caller-supplied `manual_expiration`).
3. Computes deterministic strategy candidates (`analytics/options/strategy_candidates.py`),
   ranks them (`analytics/decision/strategy_scoring.py`), attaches probability/reliability
   metrics (`analytics/decision/probability.py`), attaches explanations
   (`analytics/decision/reasoning.py`).
4. Returns an in-memory `DecisionResult` — **persistence is a separate, explicit step**
   (`services/decision_history.py::persist_decision`), called only after generation succeeds.

This separation is exactly what makes Phase 4 tractable: the scheduler-driven benchmark flow
can call `generate_decision()` unmodified (with `risk_profile=RiskProfile.MODERATE`, no
`trade_budget` override at generation time — sizing for the fixed $2,000 benchmark happens
against the *frozen* recommendation, not by changing generation inputs) and write the result
into a **new** persistence function (`persist_decision_snapshot`), instead of `persist_
decision`. The pure logic — expiration scoring, risk-profile gating, probability math, ranking,
reasoning — needs zero new code for Phase 4. Only orchestration and persistence are new.

## 3. Existing sub-engines (Expiration / Risk Profile / Probability / Explanation / Track Record)

All four "engines" the phase brief asks about are real, tested, and were live-verified against
AVGO/WMT/AAPL/NVDA/MU this session. Summary of what each *is*, since Phase 4 reuses every one
of them as a library:

- **Expiration Engine** (`analytics/options/expiration_selection.py` +
  `services/expiration_engine.py`) — `resolve_auto_expiration`/`resolve_manual_expiration`,
  scoring real, provider-discovered candidate expirations (Event Fit, Liquidity, Quote
  Coverage, Bid/Ask Quality, DTE Suitability, Data Quality). Endpoint: `GET /research/{symbol}/
  expirations?mode=auto|manual`. **Note the scope boundary already documented in this
  codebase**: `generate_decision()`'s own Auto mode still uses the older, simpler `resolve_
  best_actionable_option_market` resolver, not this engine's scored comparison — Strategy Lab
  and the standalone `/expirations` endpoint are the only current callers of the *scored*
  comparison. Phase 4's benchmark flow should decide explicitly which one it wants (the phase
  brief's §5 example ("Auto Recommended... Score: 90/100") describes the *scored* engine, so the
  benchmark flow should call `resolve_auto_expiration` directly, not rely on `generate_decision`'s
  internal resolver, and pass the result in via `manual_expiration` once selected).
- **Risk Profile** (`analytics/decision/risk_profile.py`) — `RiskProfile.{CONSERVATIVE,
  MODERATE, AGGRESSIVE}`, `MIN_BID_ASK_COVERAGE = {0.80, 0.40, None}`, default max-risk-
  utilization `{15%, 30%, 50%}` of trade budget. The benchmark portfolio's "Moderate" tier maps
  directly onto `RiskProfile.MODERATE` with zero new code — confirmed exact values, not
  approximated.
- **Probability Engine** (`analytics/decision/probability.py`) — Historical Move Compatibility,
  Estimated Probability with a Wilson confidence interval, `LOW_SAMPLE_THRESHOLD=20`, and an
  explicitly `Unavailable` True Strategy Win Rate (system-wide N=0 today because no real
  entry/exit option prices exist — **Phase 4 changes this number for the first time**, once
  `decision_snapshot` rows actually settle with real P&L).
- **Explanation Engine** (`analytics/decision/reasoning.py`) — `build_expiration_bullets`,
  `build_strike_bullets`, `build_risk_profile_fit_bullets`, `build_why_not_alternative_
  bullets`. All pure functions over already-computed data; reused as-is.
- **Track Record system** (`services/track_record.py`, `GET /research/track-record`) —
  Directional Accuracy, Bullish/Bearish Accuracy, Volatility-View Accuracy, Breakeven Success,
  Confidence Calibration buckets. **Scoped specifically to `ai_decision_version` rows** (the
  user-driven manual-decision journal). Phase 4's Track Record Analytics (§11) is explicitly a
  richer, separate module over `decision_snapshot` rows (R-multiple, expectancy, profit factor,
  max drawdown, probability calibration, strategy/DTE/confidence/risk-profile/IV-regime
  filters) — not an extension of this one. The two dashboards will show genuinely different
  numbers for a reason: one grades "was the AI's call right," the other grades "would a real
  $2,000 following this AI actually have made money." Conflating them would be a real honesty
  regression, not a simplification.

## 4. Existing scheduler/background job system

**There isn't one, functionally.** Confirmed by exhaustive search:

- `apscheduler>=3.10` is declared in `pyproject.toml` and never imported anywhere in `src/`.
- `docker-compose.yml` has exactly four services (`db`, `migrate`, `backend`, `frontend`) — no
  scheduler/worker/cron container.
- `.github/workflows/ci.yml` has no `schedule:` trigger.
- The only "scheduled" things in the codebase are two standalone scripts (`ingestion/collect_
  options_snapshots.py`, `ingestion/capture_close_snapshot.py`) whose own docstrings say "run
  via cron" or "via a user-owned cron/launchd job" — i.e., **the actual recurring trigger, if
  any exists at all, lives outside this repository**, undocumented, on whichever machine the
  project owner runs it from. Both scripts also hardcode `TICKERS = ["NVDA", "AMD", "MU",
  "SNDK"]` — the original V1 four-ticker scope, now stale relative to the rest of the app
  (which resolves any US ticker on demand).
- The only in-process trigger for *anything* today is `fastapi.BackgroundTasks` inside `POST
  /research/{symbol}/prepare` — fire-and-forget, single-ticker, purely reactive to an HTTP
  request. Not a scheduler.

### 4.1 What Phase 4 actually needs from a scheduler

Four genuinely different cadences:

| Job | Cadence | Must run even if... |
|---|---|---|
| Finnhub calendar sync | Daily, 00:00 UTC | nothing else changed that day (idempotent upsert) |
| Eligibility scan + "who reports today" | Daily, once, before the trading day | the calendar sync ran hours earlier |
| Entry capture + AI decision generation | Precisely 15:55 ET, only on days with an eligible entry due (session-dependent — see §4.3) | IBKR happens to be authenticated (real risk: it may not be) |
| Exit capture + settlement | Precisely 15:55 ET, T+1 trading day after each entered event | same IBKR risk, plus needs to correctly skip weekends/holidays |

### 4.2 Recommendation: `AsyncIOScheduler` inside `api/main.py`'s `lifespan()`

`api/main.py` already has a `lifespan()` context manager (currently only loads the embedding
model once at startup). The `backend` container is the only component docker-compose marks
`restart: unless-stopped` and gates the frontend's health check on — i.e., the only thing
actually guaranteed to be running continuously. Starting `apscheduler.schedulers.asyncio.
AsyncIOScheduler` there, with jobs registered via `add_job(..., trigger="cron", ...)` in
`America/New_York` for the 15:55 triggers and UTC for the calendar sync, requires zero new
infrastructure (no new container, no new dependency) and is the natural, minimal-risk choice.
Each job function must open its own fresh `SessionLocal()` (exactly the pattern `_run_
preparation_background` already uses for background work outside a request's own session
lifetime) and must be defensive — one company's IBKR/LLM failure must never abort the whole
day's run for every other company, mirroring `prepare_company_research`'s existing
per-step-catches-its-own-failure design.

**Real, stated risk**: if the `backend` container restarts (deploy, crash, host reboot) exactly
at 15:55 ET, that day's entry/exit job is missed entirely — apscheduler's in-memory job store
does not persist across restarts by default. A `SQLAlchemyJobStore` (apscheduler ships one)
against the same Postgres instance removes this risk cheaply and should be part of the
implementation, not an afterthought.

### 4.3 The BMO/AMC entry-timing question (flagged, not silently resolved)

The phase brief's own worked example only covers AMC:

> Earnings: Monday AMC → Exit: Tuesday 15:55 ET

For a **BMO** event reporting on day *D*, the announcement has already happened before 09:30 ET
on day *D* — capturing "entry" at day *D*'s 15:55 ET would be capturing a price that already
reflects the earnings reaction, which is exactly the look-ahead bias this phase exists to
prevent. The correct rule, stated explicitly so it can be confirmed or corrected before
implementation:

- **AMC event on day D** → entry capture at **D's** 15:55 ET (correct, matches the brief's
  example) → exit at **D+1 trading day's** 15:55 ET.
- **BMO event on day D** → entry capture must happen at **D-1 trading day's** 15:55 ET (the
  last real pre-earnings moment) → exit at **D's** 15:55 ET (same day, after the market has had
  a full session to react).
- **DMH (during market hours) event on day D** — genuinely ambiguous; Finnhub's calendar does
  distinguish this session but does not say *when* during the day. Recommendation: treat DMH
  like BMO (entry the prior day) since that is the conservative, always-correct-direction
  choice — entering too early is honest (still real pre-event data); entering after the event
  already started would not be. This should be confirmed, not assumed, before the scheduler is
  built.

This means the "eligibility scan + who reports today" job (§4.1) actually needs to look at
**two** dates every time it runs: today's AMC/DMH reporters (entry today) and tomorrow's BMO
reporters (entry today too, exit tomorrow) — not just "today's list" as a single flat query.

## 5. Existing provider architecture

`providers/base.py` declares six abstract interfaces: `MarketDataProvider`,
`OptionsDataProvider`, `EarningsDataProvider`, `EarningsEstimatesProvider`, `FilingsProvider`,
`TranscriptProvider`. Every concrete adapter is a thin `httpx`-based class (no vendor SDKs
anywhere in this codebase — Alpha Vantage, Tiingo, SEC EDGAR, and IBKR are all hand-rolled
against `httpx` + `tenacity` retries), wrapped at construction time by `services/usage_
instrumentation.py::instrument_data_provider` for cost/latency logging, with API keys resolved
through `services/secret_store/resolver.py::resolve_secret` (DB override → env var, never a
direct `settings.<x>_api_key` read).

**Confirmed gap**: `EarningsDataProvider.get_earnings_calendar(ticker) -> list[EarningsCalendarEntry]`
is single-ticker-scoped and has **zero real implementations** (only a test-only fixture
provider). `EarningsEstimatesProvider.get_next_earnings_date(ticker)` (the one actually wired
into production, via Alpha Vantage) is likewise single-ticker. **No existing interface
supports "who reports earnings in this date range, across the whole market" — a Finnhub
adapter needs new surface area, not an implementation of something that already exists.**

Two dead-but-suggestive breadcrumbs in `core/config.py`: `options_data_api_key` and
`earnings_calendar_api_key` are both declared on `Settings` and **wired to nothing** (no
`resolve_secret` entry, no reader anywhere). `earnings_calendar_api_key` in particular looks
like a placeholder from when Finnhub was originally evaluated (see `docs/data_sources.md`:
*"Finnhub | ... | Superseded by Alpha Vantage's EARNINGS_ESTIMATES/EARNINGS_CALENDAR (Phase
12)"* — Phase 4 is a deliberate reversal of that earlier decision, for the calendar use case
specifically). Recommendation: **remove `earnings_calendar_api_key`** (dead, misleading) and
add a fresh `finnhub_api_key: str | None = None`, registered in `secret_store/environment_
store.py::_ENV_ATTR["finnhub"]`, following the exact `tiingo_api_key`/`alpha_vantage_api_key`
pattern.

## 6. Components that can be reused as-is

- `generate_decision()` and its entire pure-computation core (§2/§3) — zero changes.
- `RiskProfile`, `analytics/decision/risk_profile.py` — zero changes.
- `analytics/options/expiration_selection.py` + `services/expiration_engine.py` — zero changes,
  called directly by the new scheduler job.
- `analytics/decision/probability.py` — zero changes; its `LOW_SAMPLE_THRESHOLD`/Wilson-CI
  logic is generic over any breakeven-vs-historical-move comparison and applies unchanged to
  `decision_snapshot` rows.
- `analytics/decision/reasoning.py` — zero changes.
- The provider factory pattern (`providers/factory.py`) — new Finnhub adapter follows its
  established shape (private `_build_*` returns `None` when unconfigured, public `build_*`
  wraps in a chain if needed) even though, per §5, it needs a genuinely new interface.
- The `httpx` + `tenacity` retry shape every existing adapter uses (`observability/http_client.
  new_http_client`, the `_retryable(exc)` pattern checking `TransportError`/429/5xx) — the
  Finnhub adapter should look and behave like `alpha_vantage_estimates.py`, not like a new
  pattern.
- `services/usage_instrumentation.py::instrument_data_provider` — wrap the Finnhub provider the
  same way every other provider is wrapped, for free cost/latency observability.
- `AnnouncementTime` enum (extended with `DURING_MARKET`, see §1.2) — one enum, shared.
- The `research_preparation_job` pattern (status enum + JSON step list + started_at/completed_
  at/error) — good template for a new `calendar_sync_run`/`settlement_run` audit-log table if
  one is wanted (optional; `provider_usage_event`'s simpler append-only-log shape is an
  equally valid, lighter template — see §7).
- Frontend: `Dashboard.tsx`'s fan-out fetch pattern, `DecisionTab.tsx`'s `StrategyDecisionCard`/
  `ProbabilityCard` rendering (adapted to a read-only, no-controls variant for a frozen
  snapshot), `StrategyLabTab.tsx`'s `ExpirationSelector` comparison-table component (reused
  directly to show "AI recommended vs. alternatives" on a calendar/snapshot detail view), the
  `TrackRecord.tsx` page's async-fetch → stat-card-grid → filterable-table structure as a
  layout template for the new Benchmark Portfolio dashboard (not its data — new endpoint, new
  types).
- `docker compose`'s `db`/`migrate`/`backend`/`frontend` shape — unchanged; the scheduler lives
  *inside* `backend`, not as a new service (see §4.2).

## 7. Components requiring new tables

Four new tables, one enum extension, one new config table:

### 7.1 `earnings_calendar_event` (new)

```
id (PK)
symbol (str, indexed) -- NOT required to already have a company row
company_id (FK -> company.id, nullable, indexed) -- populated lazily once a company row exists
company_name (str)
logo_url (str, nullable)
fiscal_year (int, nullable) -- Finnhub sometimes omits this; kept nullable, unlike earnings_event
fiscal_quarter (int, nullable)
earnings_date (date, indexed, NOT NULL)
session (AnnouncementTime enum: BEFORE_MARKET | AFTER_MARKET | DURING_MARKET | UNKNOWN)
eps_estimate (numeric, nullable)
revenue_estimate (numeric, nullable)
market_cap (numeric, nullable) -- populated by the eligibility filter step, not the calendar sync itself
eligibility_status (new enum: PENDING | ELIGIBLE | SKIPPED, default PENDING)
eligibility_reason (str, nullable) -- e.g. "Market cap below $10B", persisted so a past
                                       eligibility decision is never silently recomputed differently later
eligibility_checked_at (datetime, nullable)
source_provider (str, default "finnhub")
created_at / updated_at (TimestampMixin)
UNIQUE (symbol, fiscal_year, fiscal_quarter)
```

Upsert key mirrors `earnings_event`'s own `(company_id, fiscal_year, fiscal_quarter)`
convention, substituting `symbol` for `company_id` since a company row may not exist yet. This
directly satisfies the phase brief's own worked example — same symbol, same fiscal period,
`earnings_date` changes from 2026-10-29 to 2026-10-30, `ON CONFLICT ... DO UPDATE SET
earnings_date = EXCLUDED.earnings_date` — without ever creating a duplicate row. (When Finnhub
omits `fiscal_year`/`fiscal_quarter` for a given entry, the fallback upsert key is `(symbol,
earnings_date)`, treating date-only entries as their own event.)

### 7.2 `benchmark_portfolio` (new, small config table — not a singleton by schema, one row by policy)

```
id (PK), name (str), capital (numeric), risk_profile (str), expiration_mode (str, default "auto"),
is_active (bool, default True), created_at
```

The phase brief explicitly anticipates more than one portfolio existing eventually ("Do NOT mix
different portfolio sizes **initially**") — a real config table with exactly one seeded row
($2,000 / Moderate / Auto) is more honest about that than hardcoding constants that would need
a migration to ever change.

### 7.3 `decision_snapshot` (new — the immutable core of this phase)

```
id (PK)
portfolio_id (FK -> benchmark_portfolio.id, indexed)
calendar_event_id (FK -> earnings_calendar_event.id, indexed)
company_id (FK -> company.id, indexed)

-- Earnings information (frozen copies, never re-derived via FK at read time)
ticker (str), company_name (str), earnings_date (date), earnings_session (str),
generated_timestamp (datetime)

-- Market snapshot at generation time
underlying_price (numeric), atm_iv (numeric, nullable)
option_chain_snapshot (JSON) -- the real chain used, for full audit
expiration_candidates (JSON) -- the real ExpirationSelectionResult (all real alternatives, never hidden)
selected_expiration (date)

-- AI decision (mirrors ai_decision_version's proven JSON-blob shape)
strategy_category (str), legs (JSON), analysis (JSON),
risk_profile (str), score (int), score_components (JSON),
estimated_probability (JSON), historical_compatibility (JSON),
why_this_strategy (JSON), why_this_expiration (JSON), why_these_strikes (JSON),
why_not_alternative (JSON)

-- Version control (phase brief explicit requirement)
strategy_engine_version (str), model_version (str), prompt_version (str)

-- Entry capture (new capability -- does not exist anywhere in this codebase today)
entry_status (new enum: PENDING | CAPTURED | FAILED | SKIPPED, default PENDING)
entry_timestamp (datetime, nullable)
entry_underlying_price (numeric, nullable)
entry_leg_prices (JSON, nullable) -- [{option_type, action, strike, bid, ask, entry_price}], entry_price
                                      = ASK for a long leg, BID for a short leg (phase brief's own rule)
entry_capture_error (str, nullable)

-- Exit / settlement (new capability)
status (new enum: PENDING_ENTRY | ENTERED | SETTLED | VOID, default PENDING_ENTRY)
exit_status (new enum: PENDING | CAPTURED | FAILED | SKIPPED, default PENDING)
exit_timestamp (datetime, nullable)
exit_underlying_price (numeric, nullable)
exit_leg_prices (JSON, nullable) -- same shape, exit_price = BID for a long leg, ASK for a short leg
exit_capture_error (str, nullable)

-- P&L (only ever computed once, at settlement, from real captured prices)
pnl (numeric, nullable)
return_pct (numeric, nullable)
r_multiple (numeric, nullable)
is_win (bool, nullable)
capital_allocated (numeric, nullable) -- fraction of portfolio.capital actually sized to this trade
capital_utilized_pct (numeric, nullable)

created_at (TimestampMixin) -- everything above is written by exactly two service functions,
                               each called at most once per row: freeze_decision_snapshot() and
                               capture_exit_and_settle() (see §1.5 on this being a convention,
                               not yet a DB-enforced guarantee)
```

No unique constraint beyond the FK relationships — a given `calendar_event_id` should have at
most one `decision_snapshot` per `portfolio_id` in practice (enforced at the service layer:
"has this event already been frozen for this portfolio" is checked before generation, not
relied on as a DB constraint, since a legitimate re-run-after-a-crash needs to distinguish
"already frozen, skip" from "never attempted, retry" without a hard uniqueness error getting in
the way).

### 7.4 New enum members / new enums (all in `models/enums.py`)

- `AnnouncementTime` gains `DURING_MARKET = "during_market"` (extends existing enum, see §1.2).
- `UpcomingEarningsDateSource` gains `FINNHUB = "finnhub"` (extends existing enum, see §1.3).
- New `CalendarEligibilityStatus`: `PENDING | ELIGIBLE | SKIPPED`.
- New `CaptureStatus` (shared by entry and exit): `PENDING | CAPTURED | FAILED | SKIPPED`.
- New `DecisionSnapshotStatus`: `PENDING_ENTRY | ENTERED | SETTLED | VOID`.

### 7.5 What does *not* need a new table

- Real per-contract quotes used for entry/exit can be persisted into the **existing**
  `options_snapshot` table (tagging a new `OptionsSnapshotPurpose` value, e.g. `BENCHMARK_
  ENTRY`/`BENCHMARK_EXIT`, if full per-contract audit trail is wanted beyond what `decision_
  snapshot.entry_leg_prices`/`exit_leg_prices` already stores) rather than inventing a parallel
  quote table.
- Track-record analytics (§11) are **computed on read** from `decision_snapshot` rows, exactly
  like `track_record.py` computes today from `ai_decision_version` rows — no new aggregate/
  materialized table needed for V1, given this project's stated scale ("a personal research
  tool with a small real decision count").

## 8. Migration strategy

Follow the existing convention exactly (confirmed from 21 prior migrations and this session's
own two V3 migrations): one Alembic migration per logically-complete schema change, generated
via `alembic revision --autogenerate`, then **manually stripped of the known autogenerate false
positive** (`ix_document_chunk_embedding_hnsw`/`ix_document_chunk_text_fts` index drops — a
recurring artifact from SQLAlchemy not recognizing custom pgvector/FTS indexes created via raw
SQL, confirmed present in every prior migration review this session). Planned sequence:

1. `add_during_market_to_announcement_time` — enum value addition (Postgres requires `ALTER
   TYPE ... ADD VALUE` outside a transaction block in older PG, but PG16 — this project's
   version — supports it inside a transaction as of PG12+; still worth a dedicated, isolated
   migration rather than bundling it with a table-creation migration, in case a rollback is ever
   needed).
2. `add_finnhub_to_upcoming_earnings_date_source` — same shape, isolated.
3. `add_earnings_calendar_event_table` — new table (§7.1) + its own new enums (§7.4).
4. `add_benchmark_portfolio_table` — new table (§7.2), seeded with exactly one row
   ($2,000/Moderate/Auto) via a data migration in the same revision (matching this project's
   existing precedent of seed-data-in-migration for singleton config rows, e.g. `app_provider_
   settings`).
5. `add_decision_snapshot_table` — new table (§7.3) + remaining new enums (§7.4).

Each migration gets its `upgrade()`/`downgrade()` verified locally against the disposable test
Postgres (`edl-test-db`, port 5434) before being considered done, matching this session's V3
practice.

## 9. API design

New router `api/routers/earnings_calendar.py` (prefix `/earnings-calendar`, mirroring the
existing per-domain router convention — `companies.py`, `earnings.py`, `research.py`, etc., each
registered via `app.include_router(..., prefix="/api/v1")` in `api/main.py`):

```
GET  /api/v1/earnings-calendar
       ?from=&to=&session=&eligibility=  -- list, filterable by date range/session/eligibility
GET  /api/v1/earnings-calendar/{symbol}  -- one symbol's calendar entries
POST /api/v1/earnings-calendar/sync      -- manual/admin trigger of the same job the 00:00 UTC
                                             scheduler runs (idempotent, safe to call any time)
```

New router `api/routers/benchmark_portfolio.py` (prefix `/benchmark-portfolio`):

```
GET /api/v1/benchmark-portfolio                     -- portfolio config (capital, risk profile, expiration mode)
GET /api/v1/benchmark-portfolio/decisions            -- list decision_snapshot rows, paginated,
                                                          filterable by status/ticker/date range
GET /api/v1/benchmark-portfolio/decisions/{id}       -- one frozen decision snapshot, full detail
                                                          (never an edit endpoint -- no PUT/PATCH exists)
GET /api/v1/benchmark-portfolio/track-record
       ?strategy=&confidence_bucket=&dte=&risk_profile=&iv_regime=
                                                       -- the new rich analytics (§11), filterable
GET /api/v1/benchmark-portfolio/calibration           -- dedicated probability-calibration data
                                                          (predicted-bucket vs. realized-rate pairs)
```

No `POST`/mutation endpoints for `decision_snapshot` beyond what the scheduler itself calls
internally — this is deliberate: the phase brief's "never modify historical decisions" and
"never fabricate performance" constraints are best enforced by *not exposing a write path* at
all from the API surface, rather than exposing one and trusting callers not to use it. If a
manual "force a decision snapshot for ticker X today" trigger is wanted for testing/demo
purposes, it should require the same eligibility gate the real scheduler enforces and should be
clearly labeled as a manual/debug action, matching this codebase's existing `POST /research/
{symbol}/refresh` (`force=True`) pattern rather than becoming a silent backdoor.

## 10. Frontend design

Two new pages, following the existing `App.tsx` route-registration convention:

- **`pages/EarningsCalendar.tsx`** at `/earnings-calendar` — calendar/list view (phase brief
  §1's mockup: logo, ticker, company name, earnings date + session, AI status). Reuses `Dashboard.
  tsx`'s async-fan-out-fetch pattern conceptually, but against the new list endpoint (§9) rather
  than fanning out `getResearchOverview` per company (that pattern doesn't scale to "every
  eligible company reporting in the next 12 months" and doesn't cover not-yet-researched
  tickers, exactly as the frontend research agent's report flagged).
- **`pages/BenchmarkPortfolio.tsx`** at `/benchmark-portfolio` — the phase brief's §13 dashboard
  (capital, risk, total decisions, win rate, average R, expectancy, profit factor, max drawdown,
  probability calibration). **New page, not a tab added to the existing `TrackRecord.tsx`** —
  per §3's reasoning, mixing "AI directional accuracy" and "benchmark portfolio P&L" on one page
  would blur a distinction this project has been careful to keep explicit everywhere else
  (Historical Compatibility vs. Estimated Probability vs. True Strategy Win Rate is the exact
  same kind of careful separation, done once already this session).
- A decision-snapshot detail view (either its own route `/benchmark-portfolio/decisions/{id}`
  or a modal/expand-in-place from the list) reuses `DecisionTab.tsx`'s `StrategyDecisionCard`/
  `ProbabilityCard` rendering components, adapted to a **read-only** variant (no risk-profile
  selector, no "Generate" button, no expiration-mode toggle — the whole point is that this is
  frozen) plus `StrategyLabTab.tsx`'s `ExpirationSelector` table (reused directly, since it
  already renders "recommended + real alternatives + scores" exactly as the phase brief's §5
  mockup describes, and it already never hides alternatives).
- New types in `types/api.ts`: `EarningsCalendarEvent`, `BenchmarkPortfolio`, `DecisionSnapshot`
  (mirroring the new backend schema), `TrackRecordAnalytics` (R-multiple/expectancy/profit-
  factor/drawdown/calibration-bucket shape, distinct from the existing `TrackRecord` type).
- New client methods in `api/client.ts`: `getEarningsCalendar`, `getBenchmarkPortfolio`,
  `getBenchmarkDecisions`, `getBenchmarkDecision(id)`, `getBenchmarkTrackRecord(filters)`,
  `getProbabilityCalibration`.
- Nav: two new top-level links alongside the existing `AI Research`/`Cross-Company Replay`/`AI
  Track Record` set in the sidebar (`Layout`/nav component — not reviewed in file-level detail
  this pass, but confirmed to exist as a shared shell wrapping every route per `App.tsx`'s single
  nested `<Route element={<Layout />}>`).

## 11. Testing strategy

Follows this project's established discipline exactly (pytest + ruff + mypy on every backend
change, tsc + eslint + build on every frontend change, Playwright E2E against a deterministic
fixture backend with zero live-provider dependency — all confirmed working this session for V3).

**Backend unit tests, by new module:**
- Finnhub calendar adapter: mock `httpx` responses (matching the `pytest-httpx` pattern already
  used for Alpha Vantage/Tiingo tests) — real response shape parsing, error handling, no live
  network call in CI.
- Calendar upsert service: given a fixture calendar response with a changed `earnings_date` for
  an existing `(symbol, fiscal_year, fiscal_quarter)`, assert the row is updated in place, never
  duplicated (directly testing the phase brief's own worked example).
- Eligibility filter: market-cap threshold, US-listed check, options-availability check — each
  as an independent pure function with table-driven pass/fail cases, plus the "store the event,
  skip AI decision generation, persist the honest reason" integration case.
- Entry/exit capture: given a fixture options provider (reusing `FixtureOptionsProvider`/
  `OPTIONS_PROVIDER=fixture` exactly as the V3 E2E suite does), assert leg prices use ASK for
  long legs and BID for short legs at entry, BID/ASK swapped at exit (the phase brief's own
  conservative-pricing rule), and assert a provider failure produces `FAILED`/`entry_capture_
  error`, never a fabricated price.
- Settlement/P&L math: PnL, Return %, R-Multiple, Win/Loss, Capital Utilization — table-driven
  cases including a losing trade, a breakeven trade, and a capped-max-loss trade.
- Track-record analytics: Total/Settled Decisions, Win Rate, Average/Median R, Expectancy,
  Profit Factor, Max Drawdown — each independently verifiable against a hand-computed fixture
  set (mirroring how the Wilson-CI test this session was cross-checked against a manually
  computed reference value).
- Probability calibration: given a fixture set of decisions with `estimated_probability` buckets
  and known real outcomes, assert the calibration computation (predicted-bucket-midpoint vs.
  realized rate) matches hand-computed expectations.
- Scheduler jobs: each job function tested as a plain callable (apscheduler's own trigger
  mechanism is not what's under test — the job logic is), including the BMO/AMC/DMH entry-day
  logic from §4.3 as its own explicit test matrix.

**Frontend**: `tsc --noEmit`, `eslint`, `vite build` clean on every change, matching this
session's standard.

**Playwright E2E** — extends the existing deterministic-fixture pattern
(`frontend/e2e/global-setup.ts`, `backend/scripts/seed_e2e_fixtures.py`,
`OPTIONS_PROVIDER=fixture`) rather than inventing a new one:
1. Earnings calendar rendering — seed 2-3 fixture `earnings_calendar_event` rows (one eligible,
   one skipped), assert both render with their real, distinct status.
2. Finnhub synchronization — covered at the backend unit-test level (§ above); an E2E test here
   would need to mock an external HTTP call, which this project's E2E philosophy (real DB rows,
   never live external network) argues against — recommend backend-only coverage for this one.
3. Eligibility filtering — assert a below-threshold fixture company shows "Skipped / Market cap
   below $10B" in the UI, never a hidden/silently-dropped row.
4. Decision snapshot creation — seed a frozen `decision_snapshot` fixture row directly (bypassing
   the scheduler, exactly as the V3 suite bypasses live IBKR), assert the read-only detail view
   renders every required section (market snapshot, AI decision, why-bullets, version info) with
   no edit controls present.
5. Settlement calculation — seed a settled fixture row with known entry/exit prices, assert the
   UI shows the exact expected P&L/R-multiple/win-loss, matching a hand-computed value.
6. Track record dashboard — seed several settled rows spanning wins/losses/different strategies,
   assert Win Rate/Average R/Expectancy/Profit Factor/Max Drawdown all render and are internally
   consistent (e.g., profit factor recomputed from the same rows independently in the test
   matches what's displayed).

---

## Summary: development-order checklist

Matches the phase brief's own Phase 1–11 ordering exactly; nothing here proposes reordering it.
This document *is* Phase 1. Phases 2–11 each get their own commit(s) with passing tests, not one
final drop, per this project's established practice.
