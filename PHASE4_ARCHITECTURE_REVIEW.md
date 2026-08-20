# Phase 4 Architecture Review — Forward-Testing Infrastructure

Status: **review only. No backend/frontend/database/API code changed. No migrations created.
Implementation has not started.**

Branch: `feature/ai-earnings-forward-test`, currently **6 commits behind `main`** — all six are
V3.0.0 presentation/CI work (`ci: finalize V3.0.0 continuous integration workflow`,
`docs: finalize V3.0.0 release presentation`, the CORS `localhost:\d+` regex fix, and three
README/screenshot commits). None of the six touch a model, service, provider, or router this
review depends on, so every finding below is accurate as of `main`'s current tip too — but this
branch should be **rebased onto `main` before real implementation begins** (item 1 of the
implementation order below), so Phase 4 work never has to be re-diffed against a moving V3
baseline later.

This is not the first pass at this review. `ARCHITECTURE_REVIEW_PHASE4.md` (750 lines, already
committed on this branch) did the exhaustive column-by-column schema archaeology and made most
of the real design calls already. This document restructures that analysis around the eight
sections asked for this time, verifies every claim against the code as it stands *today*
(not as it stood when that document was written), and — critically — folds in two things that
have actually been *built* since: a real Finnhub provider and a real BMO/AMC/DMH timing module.
Where this document's conclusion matches the prior one, it says so briefly and cites it rather
than re-deriving it. Where new code changes the picture, that's called out explicitly.

**Addendum (2026-08-20):** §2.3 below originally recommended inline entry/exit columns on
`decision_snapshot` over separate tables, for the reasons given there at the time. That
recommendation was reviewed and explicitly overridden: this project is an AI-assisted decision
engine whose core value is a trustworthy, inspectable research record, not a CRUD app optimizing
for the fewest joins — immutable research history, point-in-time reproducibility, future ML
evaluation, and hedge-fund-style auditability outrank query convenience. §2.3 has been rewritten
in place to reflect the adopted design. Open question #1 in the summary below is marked
**resolved**, not deleted, so the reversal itself stays part of this document's own audit trail.

---

## 1. Current decision lifecycle

Verified directly against the running code (`backend/src/api/routers/research.py`,
`backend/src/services/`), not assumed:

| Step | Entry point | What happens |
|---|---|---|
| Research creation | `POST /research/{symbol}/prepare`, `POST /research/{symbol}/refresh` | Fire-and-forget `BackgroundTasks` job (`_run_preparation_background`) fetches filings, prices, options — reactive to one HTTP call, single ticker. |
| AI thesis generation | `POST /research/{symbol}/thesis` | LLM + RAG over ingested SEC filings, produces a citation-grounded thesis. |
| Strategy generation | `POST /research/{symbol}/decision` → `generate_decision()` at [`decision_engine.py:339`](backend/src/services/decision_engine.py) | Resolves the option market, computes strategy candidates, ranks them, attaches probability/reliability and why-bullets. Returns an in-memory `DecisionResult` — pure computation, no DB write. |
| Decision persistence | `services/decision_history.py::persist_decision()` | Explicit, separate step called only after generation succeeds. Writes one `AIDecisionVersion` row, `status=OPEN`. |
| Decision journal | `list_decisions`, `get_decision`, `mark_final` (same file) | Browsable history of `AIDecisionVersion` rows; `mark_final` sets `is_final=True` — a user curation flag, not a settlement state. |
| Settlement process | `POST /research/{symbol}/decisions/{id}/settle` → `services/decision_settlement.py` | `find_settlement_event()` locates the first `EarningsEvent` with a real `PriceReaction.next_day_move_pct` on or after the decision's `created_at`. Computes `direction_correct`, `actual_move_exceeded_implied`, `breakeven_met` directly on the *same* `AIDecisionVersion` row (never a new row). **`strategy_pnl` and `strategy_pnl_available` are permanently `None`/`False`** — the module's own docstring is explicit that this is deliberate, not a bug: *"This project does not capture real point-in-time entry/exit option premiums for expired contracts."* |
| Track record calculation | `services/track_record.py` (imports `AIDecisionVersion` directly) | Computed on read, not cached: Directional Accuracy, Bullish/Bearish Accuracy, Volatility-View Accuracy, Breakeven Success, Confidence Calibration buckets. |

**What exists:** a complete, tested, honest single-ticker pipeline — a human asks for research on
one company, gets an AI decision, and can later settle its *directional* correctness once real
price data exists. 9 backend test modules already cover this exact path
(`test_services_decision_history.py`, `test_services_decision_settlement.py`,
`test_services_track_record.py`, plus 6 `test_analytics_decision_*.py` files).

**What is missing** (each one is a real, verified absence, not a guess):
1. **No automation.** Every step above is triggered by a human or an API caller. There is no
   process that runs unattended.
2. **No cross-symbol awareness.** The system only knows a company exists once someone researches
   it by ticker. It cannot answer "who reports earnings this week."
3. **No real option P&L, anywhere.** By design (see settlement row above) — this is the literal
   gap Phase 4 exists to fill.
4. **No enforced immutability.** `AIDecisionVersion`'s append-only-ness is a service-layer
   convention (nothing stops a second write to the same row) — fine for V3's low-stakes
   "settlement bolts outcome fields onto the same row once," not fine as the sole guarantee for a
   forward-test record whose entire value proposition is "this can't have been edited after the
   fact."
5. **No fixed-capital benchmark concept.** `trade_budget`/`risk_cap` on `AIDecisionVersion` are
   per-decision, user-supplied inputs, not a standing portfolio with its own identity across many
   decisions.

**What must change:** nothing in the table above needs to be *modified*. `generate_decision()`,
the risk/probability/expiration/reasoning engines, and the settlement/track-record read paths are
all reused as libraries, called from new orchestration and written into new tables. This mirrors
`ARCHITECTURE_REVIEW_PHASE4.md` §2/§6's conclusion exactly, re-confirmed against the current code:
zero changes to any file in the table above.

---

## 2. Forward-testing data model

### 2.1 Earnings Calendar Event — new table, not a reuse of `earnings_event`

`earnings_event` (`company_id, fiscal_year, fiscal_quarter` unique) is retrospective: it's
populated by SEC-XBRL backfill and its key requires knowing the fiscal period, which a
Finnhub-discovered event reporting next week may not have confirmed yet. Forcing the calendar
into it means guessing fiscal quarters or breaking the unique constraint — rejected for the same
reason `ARCHITECTURE_REVIEW_PHASE4.md` §1.2 rejected it. New table, keyed by `(symbol,
fiscal_year, fiscal_quarter)` with a `(symbol, earnings_date)` fallback when Finnhub omits the
fiscal fields:

```
id, symbol (indexed), company_id (FK company, nullable — populated lazily),
company_name, logo_url (nullable),
fiscal_year (nullable), fiscal_quarter (nullable),
earnings_date (date, indexed, NOT NULL),
session (AnnouncementTime: BEFORE_MARKET | AFTER_MARKET | DURING_MARKET | UNKNOWN),
eps_estimate, revenue_estimate (nullable),
market_cap (nullable — filled by the eligibility filter, not the sync itself),
eligibility_status (PENDING | ELIGIBLE | SKIPPED), eligibility_reason (nullable, persisted so a
  past eligibility call is never silently recomputed differently later),
source_provider (default "finnhub"), created_at, updated_at
```

### 2.2 Decision Snapshot — the immutable core

```
id, portfolio_id (FK), calendar_event_id (FK), company_id (FK),
ticker, company_name, earnings_date, earnings_session, generated_timestamp,
underlying_price, atm_iv (nullable),
option_chain_snapshot (JSON, full audit), expiration_candidates (JSON, all real alternatives),
selected_expiration,
strategy_category, legs (JSON), analysis (JSON), risk_profile, score, score_components (JSON),
estimated_probability (JSON), historical_compatibility (JSON),
why_this_strategy / why_this_expiration / why_these_strikes / why_not_alternative (JSON),
strategy_engine_version, model_version, prompt_version,
status (PENDING_ENTRY | ENTERED | SETTLED | VOID),
created_at
```

"Immutable" is enforced the same way `ai_decision_version` enforces append-only today: a
convention, not a DB constraint. As of the §2.3 revision below, `decision_snapshot` has exactly
**one** writer, `freeze_decision_snapshot()`, called once per row — entry and exit capture no
longer write to this table at all. The only field that changes after generation is the `status`
rollup (`PENDING_ENTRY → ENTERED → SETTLED`/`VOID`), an operational lifecycle field, not a
research fact — mirroring how `ai_decision_version.status` is already mutated post-generation in
V3 today without compromising that row's own generation-payload immutability. A Postgres
`BEFORE UPDATE` trigger rejecting writes to the frozen generation columns remains a reasonable
stretch goal once the core is working, not a blocker for V1.

### 2.3 Option Entry Snapshot and Settlement Snapshot — adopted as separate tables

**Decision reversed on 2026-08-20** (see the addendum at the top of this document). This section
originally recommended inline entry/exit columns on `decision_snapshot`, on query-simplicity
grounds. Overridden by explicit direction: this project prioritizes immutable research history,
point-in-time reproducibility, future ML evaluation, and hedge-fund-style auditability over the
fewest joins. `entry_snapshot` and `settlement_snapshot` are adopted as their own tables, each a
child of `decision_snapshot`:

```
decision_snapshot
        |
        +-- entry_snapshot      (0..N rows -- see "append-only attempts" below)
        |
        +-- settlement_snapshot (0..N rows -- see "append-only attempts" below)
```

**`entry_snapshot`** — what the recommended trade would have actually cost, captured at the
decision's real entry moment:

```
id (PK)
decision_snapshot_id (FK -> decision_snapshot.id, indexed -- deliberately NOT unique, see below)
attempt_number (int, default 1)
status (CaptureStatus: PENDING | CAPTURED | FAILED | SKIPPED)
captured_at (datetime, nullable)
underlying_price (numeric, nullable)
leg_quotes (JSON, nullable) -- one entry per leg:
  {option_type, action, strike, bid, ask, mid, implied_volatility,
   delta, gamma, theta, vega, entry_price}
  entry_price = ASK for a long leg, BID for a short leg (the conservative rule, computed and
  stored at capture time, never re-derived at read time)
capture_error (str, nullable)
source_provider (str, e.g. "ibkr")
created_at (TimestampMixin)
```

**`settlement_snapshot`** — the real outcome once the market has had a session to react:

```
id (PK)
decision_snapshot_id (FK -> decision_snapshot.id, indexed -- deliberately NOT unique)
attempt_number (int, default 1)
status (CaptureStatus: PENDING | CAPTURED | FAILED | SKIPPED)
captured_at (datetime, nullable)

-- underlying
underlying_exit_price (numeric, nullable)
earnings_reaction_pct (numeric, nullable)   -- the real next-day move
realized_volatility (numeric, nullable)

-- options
leg_quotes (JSON, nullable)                 -- exit quotes, same per-leg shape as entry_snapshot,
                                                exit_price = BID for a long leg, ASK for a short leg
theoretical_pnl (numeric, nullable)
return_pct (numeric, nullable)
r_multiple (numeric, nullable)
is_win (bool, nullable)
max_gain (numeric, nullable)
max_loss (numeric, nullable)
breakeven_result (str, nullable)            -- e.g. "met" | "missed", against the frozen breakeven
expiration_outcome (str, nullable)          -- e.g. "expired_otm" | "expired_itm" | "closed_early"
capital_allocated (numeric, nullable)
capital_utilized_pct (numeric, nullable)

capture_error (str, nullable)
source_provider (str, nullable)
created_at (TimestampMixin)
```

Headline metrics (`theoretical_pnl`, `r_multiple`, `earnings_reaction_pct`, etc.) are real typed
columns, not JSON — the explicit reason is the brief's own "future ML evaluation capability": a
feature-extraction job querying "every settled decision's R-multiple, bucketed by DTE" should
never have to parse JSON to do it. `leg_quotes` stays JSON because its shape is genuinely
variable (strategy leg count varies), not because it's a place to avoid designing real columns.

**Why `decision_snapshot_id` is indexed, not unique — append-only attempts, not overwritten
rows.** A hard 1:1 unique constraint would force a retry after a transient IBKR failure to either
UPDATE the existing (failed) row — silently destroying the record that an attempt failed and
when — or leave a permanently un-retriable `FAILED` row with no path forward. Neither serves
"immutable research history." Instead: every capture attempt, successful or not, is its own
permanent row, numbered by `attempt_number`, never edited or deleted. The **operative** entry (or
settlement) for a given `decision_snapshot_id` is the most recent row with `status=CAPTURED`; if
none succeeded, the most recent row's `capture_error` is the honest, permanent record of why. This
is a direct, necessary consequence of the auditability priority behind this decision, not a
separate open question — it's implemented exactly this way starting in Phase 4.1.

**Not built now, flagged for later:** a further-normalized `entry_leg_quote`/`settlement_leg_
quote` table (one row per leg, not one JSON array per snapshot) is the natural next step in this
direction if per-leg queryability independent of its parent snapshot is ever needed — V1's scale
doesn't need it, and it wasn't part of what was asked for in this decision. The existing
`options_snapshot` table remains available, unchanged, as a separate general-purpose per-contract
quote store if a raw-provider-response audit trail beyond `leg_quotes` is ever wanted — not
required by this design.

### 2.4 Benchmark Portfolio — small config table

```
id, name, capital, risk_profile, expiration_mode (default "auto"), is_active, created_at
```

One seeded row ($2,000 / Moderate / Auto) rather than hardcoded constants — cheap to make a real
row instead of a magic number, and the brief itself anticipates more than one portfolio existing
eventually.

---

## 3. Provider architecture

`providers/base.py` declares six ABCs (`MarketDataProvider`, `OptionsDataProvider`,
`EarningsDataProvider`, `EarningsEstimatesProvider`, `FilingsProvider`, `TranscriptProvider`).
Confirmed gap at review time: `EarningsDataProvider.get_earnings_calendar(ticker)` and
`EarningsEstimatesProvider.get_next_earnings_date(ticker)` are both single-ticker — neither
supports "who reports in this date range, across the whole market."

**This has already been partly built on this branch, since the prior review was written**
(commit `2b27c4c`, "Add Finnhub earnings calendar provider"):

- A **new** ABC, `EarningsCalendarProvider`, added to `providers/base.py` with the cross-symbol
  shape (`get_earnings_calendar(from_date, to_date) -> list[FinnhubCalendarEntry]`).
- A real, tested adapter: [`providers/finnhub.py`](backend/src/providers/finnhub.py) —
  `FinnhubEarningsCalendarProvider`, `httpx` + `tenacity`, same retry/error shape as every other
  adapter in this codebase (`_retryable` on transport errors / 429 / 5xx). Also implements
  `get_company_profile(symbol)` against `/stock/profile2` for name/logo/market cap — needed by the
  eligibility filter, since the calendar endpoint alone carries only a raw symbol.
- Boundary types in `providers/types.py`: `FinnhubCalendarEntry`, `FinnhubCompanyProfile`, both
  Pydantic-validated, both explicit that `session` is Finnhub's raw string (`"bmo"|"amc"|"dmh"|""`)
  — deliberately *not* mapped to `AnnouncementTime` at this layer, keeping the provider a thin,
  honest mirror of what Finnhub actually returned. Mapping is a calendar-sync-service concern.
- Config: `finnhub_api_key` added to `Settings` (`core/config.py`), registered in
  `secret_store/environment_store.py` (`"finnhub": "finnhub_api_key"`) — resolvable through the
  existing `resolve_secret` (DB override → env var) path like every other provider key. The old,
  dead `earnings_calendar_api_key` breadcrumb flagged by the prior review has been removed.
- 175 lines of real tests (`backend/tests/test_providers_finnhub.py`).
- Test coverage confirms `test_providers_finnhub.py` mocks `httpx` responses — no live network
  call in CI, matching the Alpha Vantage/Tiingo test pattern.

**Confirmed remaining gap:** `providers/factory.py` has **not** been updated. There is no
`build_earnings_calendar_provider()` function — `FinnhubEarningsCalendarProvider` is a real,
tested, importable class, but nothing in the app constructs or injects one yet. This is the
single concrete next step at the provider layer, and a small one (the factory's existing
`_build_*` shape — return `None` when unconfigured, wrap in `instrument_data_provider` for
cost/latency logging — is a direct template).

**No duplicate provider logic:** confirmed by the adapter's own docstring — it explicitly does
not touch `services/market_expectations.py` or `providers/alpha_vantage_estimates.py`. The
existing per-ticker "next earnings date" flow keeps using Alpha Vantage unchanged; Finnhub is
additive, scoped only to the new cross-symbol calendar and eligibility profile lookups.

---

## 4. Scheduling architecture

Confirmed, exhaustively, that **no working scheduler exists today**:
- `apscheduler>=3.10` is declared in `pyproject.toml` and appears nowhere else in `backend/src/`
  (`grep -rn apscheduler backend/src/` returns nothing).
- `docker-compose.yml` has exactly four services — `db`, `migrate`, `backend`, `frontend` — no
  scheduler/worker/cron container.
- `.github/workflows/ci.yml` has no `schedule:` trigger.
- The only in-process trigger anywhere is `fastapi.BackgroundTasks` inside `POST
  /research/{symbol}/prepare` — single-request, fire-and-forget, not recurring.
- `api/main.py`'s `lifespan()` today only constructs the shared embedding model and the research
  rate limiter at startup — confirmed by direct read, nothing scheduler-related.

### 4.1 Cadences needed

| Job | Cadence | Must tolerate |
|---|---|---|
| Finnhub calendar sync | Daily, 00:00 UTC | Idempotent upsert regardless of what already ran that day |
| Eligibility scan | Daily, before the trading day | Looking at **two** dates each run (see §4.3) |
| Entry capture + decision generation | 15:55 ET, only on days with an eligible entry due | IBKR Gateway not being authenticated (real, recurring operational risk — confirmed live twice during earlier phases of this project) |
| Exit capture + settlement | 15:55 ET, T+1 relative to each entered event | Same IBKR risk, plus weekend/holiday skipping |

### 4.2 Recommendation: `AsyncIOScheduler` inside `api/main.py`'s existing `lifespan()`

Evaluated against the brief's four options:

- **Celery** — needs a broker (Redis/RabbitMQ) and a separate worker process. No existing
  infrastructure for either; real new infra for a project whose own docs describe it as a
  personal-scale research tool. Rejected as over-scoped.
- **Cron** — this codebase already has exactly this anti-pattern: `ingestion/collect_options_
  snapshots.py` and `ingestion/capture_close_snapshot.py` both say "run via cron" in their own
  docstrings, and the actual trigger (if any exists at all) lives outside version control, on
  whichever machine the owner runs it from — undocumented and unverifiable. Phase 4 should not
  add a third instance of this exact problem. Rejected.
- **FastAPI lifespan alone (no scheduler library)** — `lifespan()` only runs once at startup and
  once at shutdown; it has no built-in mechanism for recurring, precisely-timed triggers. Not
  sufficient by itself, but it *is* the right place to start whichever scheduler is chosen.
- **APScheduler (`AsyncIOScheduler`)** — already a declared dependency, zero new infrastructure,
  zero new containers. Started from the existing `lifespan()` hook, inside the `backend`
  container — the only component `docker-compose.yml` marks `restart: unless-stopped` and gates
  the frontend's health check on, i.e. the only thing actually guaranteed to run continuously.
  **Recommended.**

Each job function must open its own `SessionLocal()` (the same pattern `_run_preparation_
background` already uses for work outside a request's session lifetime) and must isolate
per-company failures so one bad IBKR session doesn't abort the whole day's run — mirroring
`prepare_company_research`'s existing per-step error isolation.

**Real, stated risk, not glossed over:** APScheduler's default in-memory job store does not
survive a container restart. If `backend` restarts at exactly 15:55 ET (deploy, crash, host
reboot), that day's capture is missed entirely. `SQLAlchemyJobStore` (apscheduler ships one)
against the existing Postgres instance removes this cheaply and should be part of the initial
implementation, not a follow-up.

A missed or failed capture must produce an honest `FAILED`/`SKIPPED` status (§2.3) — never a
fabricated price. This is the direct mechanism by which "No fabricated performance" (the phase
goal's own stated constraint) gets enforced in code, not just in intent.

### 4.3 BMO/AMC/DMH entry timing — already resolved, not an open question anymore

The prior review (§4.3) flagged this as unresolved: an AMC event's 15:55 ET entry on the earnings
date itself is safe, but a BMO event has already reported *before* that same day's open — entering
"on" the earnings date would already reflect the reaction, which is exactly the look-ahead bias
this phase exists to prevent.

**This has since been built and tested** (commit `fa14e83`,
[`analytics/earnings_timing.py`](backend/src/analytics/earnings_timing.py)): a pure, dependency-free
module (`compute_entry_exit_schedule(earnings_date, session) -> EarningsEntryExitSchedule`) that:

- **AMC** on day D → entry at D's 15:55 ET → exit at the next real trading day's 15:55 ET.
- **BMO** on day D → entry at the previous real trading day's 15:55 ET → exit at D's 15:55 ET.
- **UNKNOWN or any unrecognized session value** → treated conservatively, exactly like BMO — the
  module's own docstring is explicit about why: *"never assume AMC; assuming AMC on a real BMO
  event would be a real look-ahead-bias violation, while assuming BMO on a real AMC event only
  costs one extra day of safety margin."* This resolves the prior review's own recommendation for
  DMH the same way, and extends it to any future unrecognized value too.
- Real trading-day awareness: a from-scratch NYSE holiday calculator (`us_market_holidays`,
  computed via nth-weekday/Easter-Sunday rules, not a hardcoded table that goes stale), explicitly
  more rigorous than `analytics/market_session.py`'s existing documented gap ("does NOT know about
  market holidays") — justified because a wrong trading day here is a permanent, unrecoverable
  correctness bug on a frozen snapshot, not a cosmetic live-status miss.
- 160 lines of table-driven tests already exist
  (`backend/tests/test_analytics_earnings_timing.py`).

This module is pure logic — it is not yet called from anywhere (there's no scheduler to call it
from yet), but it is real, tested, and ready to be the scheduler's single source of truth for
entry/exit dates the moment §4.2 exists.

---

## 5. Database migration plan

No migrations exist yet — this is the plan for when implementation starts, one Alembic revision
per logically-complete change, matching the existing convention (21 prior migrations, generated
via `alembic revision --autogenerate`, then manually stripped of the known false-positive index
drops that autogenerate produces around the raw-SQL pgvector/FTS indexes).

1. `add_during_market_to_announcement_time` — extend the existing `AnnouncementTime` enum with
   `DURING_MARKET`. Isolated, in case it ever needs its own rollback.
2. `add_finnhub_to_upcoming_earnings_date_source` — extend `UpcomingEarningsDateSource` with
   `FINNHUB` (the existing enum already has an unused `ESTIMATED`/`UNKNOWN` precedent for exactly
   this kind of clean addition).
3. `add_earnings_calendar_event_table` — §2.1, plus its own new `CalendarEligibilityStatus` enum.
4. `add_benchmark_portfolio_table` — §2.4, with the single seed row written as a data migration in
   the same revision (matching this project's existing precedent for singleton config rows, e.g.
   `app_provider_settings`).
5. `add_decision_snapshot_table` — §2.2 only now (generation payload + `status` rollup), plus the
   new `DecisionSnapshotStatus` enum. Smaller than originally planned — entry/exit fields have
   moved to their own tables and migrations below (§2.3, decision reversed 2026-08-20).
6. `add_entry_snapshot_table` — §2.3, plus a new shared `CaptureStatus` enum. FK to
   `decision_snapshot.id`, indexed, deliberately not unique (append-only capture attempts, see
   §2.3's reasoning).
7. `add_settlement_snapshot_table` — §2.3, reusing `CaptureStatus`. Same FK shape as
   `entry_snapshot`.

Each migration's `upgrade()`/`downgrade()` gets verified against the disposable test Postgres
(`edl-test-db`, port 5434) before being considered done, matching this session's standing
practice. No FK from any existing V3 table points *into* these new ones — nothing here can break
existing behavior even in the worst case of a bad migration being rolled back.

---

## 6. API changes

New router, `api/routers/earnings_calendar.py` (prefix `/earnings-calendar`, same
per-domain-router convention as `companies.py`/`earnings.py`/`research.py`):

```
GET  /api/v1/earnings-calendar?from=&to=&session=&eligibility=
GET  /api/v1/earnings-calendar/{symbol}
POST /api/v1/earnings-calendar/sync        -- manual trigger of the same idempotent job the
                                               00:00 UTC scheduler runs
```

New router, `api/routers/benchmark_portfolio.py` (prefix `/benchmark-portfolio`):

```
GET /api/v1/benchmark-portfolio
GET /api/v1/benchmark-portfolio/decisions?status=&ticker=&from=&to=
GET /api/v1/benchmark-portfolio/decisions/{id}
GET /api/v1/benchmark-portfolio/track-record?strategy=&confidence_bucket=&dte=&risk_profile=&iv_regime=
GET /api/v1/benchmark-portfolio/calibration
```

Matches the brief's two named examples (`GET /earnings/calendar`, `GET /decisions/history`)
under the project's existing `/api/v1/{domain}` prefix convention, plus the read paths a working
dashboard actually needs. **Since §2.3's revision, `GET /benchmark-portfolio/decisions/{id}`
composes three tables into one response** — the `decision_snapshot` row plus its operative
`entry_snapshot` and `settlement_snapshot` rows (most recent `CAPTURED` row each, per §2.3's
append-only-attempts rule) — rather than reading a single wide row. The response shape is
unaffected from the frontend's perspective; only the read query changes.

**No `POST /decisions/{id}/settle`-style mutation endpoint is proposed**, deliberately — the
brief's own worked example includes one, but the phase goal's own constraints ("never modify
historical decisions," "no fabricated performance") are better enforced by not exposing a write
path on `decision_snapshot` at all than by exposing one and trusting every caller not to misuse
it. Settlement is something only the scheduler's exit-capture job performs, internally, using the
same `entry`/`exit` capture functions §2.3 and §4 describe. If a manual "force a snapshot now, for
testing" trigger is wanted, it should require the same eligibility gate the real scheduler
enforces and be clearly labeled as a debug action — matching the existing `POST
/research/{symbol}/refresh?force=True` pattern — rather than becoming a second, quieter way to
write to this table. **Flagged as an open question in the summary below**, since the brief's
example explicitly proposed a settle endpoint and this review is recommending against it.

---

## 7. Frontend impact

Two new pages, following `App.tsx`'s existing route-registration convention (confirmed: current
routes are `company/:ticker`, `earnings/:id`, `research`, `historical-replay`, `track-record`,
`settings/*`, `system-status` — no calendar or benchmark route exists yet):

- **`pages/EarningsCalendar.tsx`** at `/earnings-calendar` — list/calendar view (logo, ticker,
  company name, date + session, AI status). New endpoint-backed fetch, not a reuse of
  `Dashboard.tsx`'s per-company fan-out pattern (that doesn't scale to "everyone reporting in the
  next year" and doesn't cover not-yet-researched tickers).
- **`pages/BenchmarkPortfolio.tsx`** at `/benchmark-portfolio` — capital, risk profile, decision
  count, win rate, average R, expectancy, profit factor, max drawdown, probability calibration.
  **A new page, not a tab bolted onto the existing `TrackRecord.tsx`** — the existing Track Record
  page grades "was the AI's directional call right" against `AIDecisionVersion` rows; this page
  grades "would a real $2,000 following the AI actually have made money" against
  `decision_snapshot` rows. They will show different numbers for a real reason; merging them would
  blur a distinction this project has kept explicit everywhere else (Historical Compatibility vs.
  Estimated Probability vs. True Strategy Win Rate is the same kind of deliberate separation).
- A read-only decision-snapshot detail view, reusing `DecisionTab.tsx`'s `StrategyDecisionCard`/
  `ProbabilityCard` components (no risk-profile selector, no Generate button — frozen means
  frozen) and `StrategyLabTab.tsx`'s `ExpirationSelector` table for showing the recommended
  expiration against its real alternatives.
- New types (`types/api.ts`): `EarningsCalendarEvent`, `BenchmarkPortfolio`, `DecisionSnapshot`,
  a distinct `BenchmarkTrackRecordAnalytics` shape (R-multiple/expectancy/profit-factor/drawdown/
  calibration — not the existing `TrackRecord` type).
- New client methods (`api/client.ts`): `getEarningsCalendar`, `getBenchmarkPortfolio`,
  `getBenchmarkDecisions`, `getBenchmarkDecision(id)`, `getBenchmarkTrackRecord(filters)`,
  `getProbabilityCalibration`.
- Nav: two new links in the existing sidebar shell (`Layout`, wrapping every route via `App.tsx`'s
  single nested `<Route element={<Layout />}>`).

No implementation in this phase — this is the design that new code will follow once §5's
migrations and §6's endpoints exist.

---

## 8. Testing strategy

Follows the project's existing, already-enforced discipline: pytest + ruff + mypy on every
backend change, tsc + eslint + build on every frontend change, Playwright E2E against a
deterministic fixture backend (`OPTIONS_PROVIDER=fixture`, confirmed as a real env-gated path in
`providers/factory.py`, deliberately excluded from the provider allowlist so it can never be
selected by accident in a real deployment) — zero live-provider dependency in CI.

**The two modules already built this phase are the concrete template for everything else:**
`test_providers_finnhub.py` (mocked `httpx`, no live network) for provider-layer tests, and
`test_analytics_earnings_timing.py` (160 lines, table-driven) for pure-logic modules — both
already passing in CI today.

**Backend unit tests, by new module:**
- Calendar upsert service: given a changed `earnings_date` for an existing `(symbol, fiscal_year,
  fiscal_quarter)`, assert the row updates in place, never duplicates.
- Eligibility filter: market-cap threshold / listing / options-availability, each as an
  independent table-driven pure function, plus the integration case ("store the event, skip
  generation, persist the honest `eligibility_reason`").
- Entry/exit capture: given a fixture options provider, assert ASK-for-long/BID-for-short at
  entry, BID/ASK swapped at exit, and assert a provider failure inserts a new `entry_snapshot`/
  `settlement_snapshot` row with `status=FAILED` + `capture_error` — never a fabricated price, and
  never an UPDATE of a prior failed row (§2.3's append-only-attempts rule).
- Settlement/P&L math: `theoretical_pnl`, `return_pct`, `r_multiple`, win/loss, `max_gain`/
  `max_loss`, `breakeven_result`, `expiration_outcome`, `earnings_reaction_pct`,
  `realized_volatility`, capital utilization — table-driven, including a losing trade, a
  breakeven trade, and a capped-max-loss trade.
- "Operative row" selection: given a `decision_snapshot` with two `entry_snapshot` attempts (one
  `FAILED`, one later `CAPTURED`), assert reads return the `CAPTURED` one, and assert the `FAILED`
  row is still present and queryable, never deleted.
- Track-record analytics: Win Rate, Average/Median R, Expectancy, Profit Factor, Max Drawdown —
  each cross-checked against an independently hand-computed fixture, the same practice already
  used for this project's Wilson-CI probability tests.
- Scheduler job functions: tested as plain callables (the trigger mechanism itself isn't what
  needs testing — the job logic is), including the BMO/AMC/DMH matrix from §4.3 as its own
  explicit test (already partially covered by `test_analytics_earnings_timing.py`; new tests only
  need to cover the job wiring, not re-test the timing math itself).

**Frontend:** `tsc --noEmit`, `eslint`, `vite build` clean on every change — the existing bar.

**Playwright E2E**, extending the existing deterministic-fixture setup (`frontend/e2e/global-
setup.ts`, `backend/scripts/seed_e2e_fixtures.py`) rather than inventing a new one:
1. Calendar rendering — seed 2–3 fixture `earnings_calendar_event` rows (one eligible, one
   skipped), assert both render with their real, distinct status.
2. Eligibility filtering — assert a below-threshold company shows "Skipped / Market cap below
   $10B," never a silently-dropped row.
3. Decision snapshot detail — seed a frozen row directly (bypassing the scheduler, exactly as the
   V3 suite bypasses live IBKR), assert every section renders with no edit controls present.
4. Settlement — seed a settled fixture row with known entry/exit prices, assert the UI shows the
   exact expected P&L/R-multiple/win-loss.
5. Track record dashboard — seed several settled rows, assert Win Rate/Average R/Expectancy/
   Profit Factor/Max Drawdown are internally consistent with an independent recomputation inside
   the test itself.

Finnhub calendar sync itself is **not** proposed as an E2E test — it would require mocking an
external HTTP call from within a Playwright run, which conflicts with this project's existing E2E
philosophy (real DB rows, never live external network). Backend-only coverage is recommended for
that path, matching how `test_providers_finnhub.py` already covers it.

---

## Summary

### 1. Architecture document
This file. Read alongside `ARCHITECTURE_REVIEW_PHASE4.md` for the exhaustive schema-by-schema
detail this document summarizes rather than repeats.

### 2. Current system gaps
- No scheduler/automation of any kind (§4).
- No cross-symbol earnings awareness — `providers/factory.py` doesn't build a Finnhub provider
  yet even though the adapter itself is real and tested (§3).
- No real option entry/exit price capture, therefore no real P&L, anywhere in the system today
  (§1, §2.3).
- No DB-enforced immutability for any decision record — convention only, now spanning three
  tables (`decision_snapshot`, `entry_snapshot`, `settlement_snapshot`) instead of one (§1, §2.2,
  §2.3).
- No fixed-capital benchmark portfolio concept (§2.4).
- Branch is 6 commits behind `main` (presentation/CI only, but should be rebased before real
  implementation work starts).

### 3. Recommended implementation order
1. Rebase `feature/ai-earnings-forward-test` onto current `main`.
2. Wire `FinnhubEarningsCalendarProvider` into `providers/factory.py` (small — the adapter and
   config already exist).
3. Migrations 1–2 (§5): enum extensions. Lowest risk, unblocks everything else.
4. `earnings_calendar_event` table + calendar-sync service + `GET /earnings-calendar*` (§2.1,
   §5.3, §6). Independently useful and testable before any scheduler exists — can be triggered
   manually via the `sync` endpoint first.
5. `benchmark_portfolio` table, seeded (§2.4, §5.4).
6. `decision_snapshot` table + `freeze_decision_snapshot()` (generation-only, no capture yet)
   (§2.2, §5.5).
7. Scheduler (`AsyncIOScheduler` + `SQLAlchemyJobStore` in `lifespan()`) wired to call
   `compute_entry_exit_schedule()` (already built) for the calendar-sync and eligibility-scan
   jobs only (§4).
8. Entry capture (§2.3) + the 15:55 ET entry job.
9. Exit capture + settlement math (§2.3) + the T+1 exit job.
10. `benchmark-portfolio` read endpoints + track-record/calibration analytics (§6).
11. Frontend: `EarningsCalendar.tsx`, `BenchmarkPortfolio.tsx`, read-only snapshot detail (§7).
12. E2E suite (§8).

### 4. Estimated complexity per phase
(Rough sizing, not a schedule commitment — S = small/contained, M = a real feature, L = the
biggest single piece of new logic in this phase.)

| Step | Size | Why |
|---|---|---|
| 1. Rebase | S | Clean, non-overlapping diffs. |
| 2. Wire Finnhub into factory | S | Adapter, config, and secret-store entry already exist. |
| 3. Enum migrations | S | Two isolated, mechanical migrations. |
| 4. Calendar table + sync + API | M | New service, new upsert logic, new router — but no scheduler dependency yet. |
| 5. Benchmark portfolio table | S | One small config table, one seed row. |
| 6. Decision snapshot (generation only) | M | Schema is fully designed (§2.2); freezing logic calls `generate_decision()` unmodified. |
| 7. Scheduler | M | New infra pattern for this repo (first real scheduler), but `AsyncIOScheduler` + `SQLAlchemyJobStore` is a well-trodden combination and the timing logic it calls is already built and tested. |
| 8–9. Entry/exit capture + settlement | **L** | The actual new capability this phase exists for — real point-in-time option pricing, real P&L math, real IBKR-reliability handling. Most of the genuinely new logic lives here. |
| 10. Analytics endpoints | M | Math is spec'd (§8); computed-on-read, no new infra. |
| 11. Frontend | M | Two new pages, but heavy reuse of existing components in read-only variants. |
| 12. E2E | M | Extends an existing, working fixture pattern; six new scenarios. |

### 5. Questions requiring confirmation before coding
1. **[RESOLVED 2026-08-20] §2.3 — Option Entry/Settlement as inline columns vs. standalone
   tables.** This review originally recommended inline columns on `decision_snapshot`. Overridden
   by explicit direction: `entry_snapshot` and `settlement_snapshot` are adopted as separate,
   append-only-attempt tables, prioritizing immutable research history and hedge-fund-style
   auditability over query simplicity. See the addendum at the top of this document and the
   revised §2.3 for the full reasoning and schema. Migrations 6–7 (§5) implement this.
2. **§6 — no settlement mutation endpoint.** The brief's own worked example
   (`POST /decisions/{id}/settle`) implies one; this review recommends against exposing any write
   path on `decision_snapshot` from the API at all, settlement being scheduler-internal only.
   Confirm this is acceptable, or specify what a manual/debug trigger should require (same
   eligibility gate? separate auth? explicit `force=True` flag, matching the existing
   `/research/{symbol}/refresh` pattern?).
3. **§3 — expiration-selection engine choice for the benchmark flow.** `generate_decision()`'s own
   Auto mode uses the older, simpler `resolve_best_actionable_option_market` resolver; the *scored*
   comparison engine (Event Fit / Liquidity / Quote Coverage / etc.) is currently reached only via
   `resolve_auto_expiration` / the Strategy Lab endpoint. The benchmark flow should almost
   certainly use the scored engine directly and pass its result into `generate_decision()` via
   `manual_expiration=`, but this is worth an explicit confirmation since it changes which code
   path the automated flow depends on.
4. **§4 — IBKR Gateway reliability.** A missed 15:55 ET capture because the Gateway session expired
   is a real, recurring, already-observed failure mode, not a hypothetical. Confirm that a
   `FAILED`/`SKIPPED` benchmark decision is an acceptable, visible product outcome (surfaced in
   the UI as an honest gap, per the phase goal's own "no fabricated performance" constraint)
   rather than something that needs a retry/alerting mechanism in V1.
5. **Branch rebase timing (§5, implementation-order item 1).** Confirm the rebase should happen
   before any Phase 4 commits are added, rather than at the end — rebasing after several new
   commits exist multiplies the conflict surface for no benefit.
