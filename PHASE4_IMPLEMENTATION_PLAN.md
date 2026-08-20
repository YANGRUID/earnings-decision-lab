# Phase 4 Implementation Plan — Forward-Testing Infrastructure

Status: **planning only — no implementation started.** This is the checklist derived from
[`PHASE4_ARCHITECTURE_REVIEW.md`](PHASE4_ARCHITECTURE_REVIEW.md); every design decision below was
made there, this document only sequences it into six shippable sub-phases, each with its own
migration(s), code, and passing tests before the next one starts — matching this project's
existing practice of one logically-complete commit per unit of work, not one final drop.

Branch: `feature/ai-earnings-forward-test`, rebased onto `main` at `20718ca` (see
`git log --oneline -6` on this branch — clean rebase, verified, no conflicts).

Five open questions from the architecture review are **not** re-litigated here; they're linked
into the specific sub-phase each one blocks, so none can be silently skipped once coding starts.

---

## Phase 4.1 — Database foundation

**Goal:** every table and enum Phase 4 needs exists, migrated, indexed, with zero rows written to
them yet. No service code in this phase — schema only.

**Migration order** (one Alembic revision each, `alembic revision --autogenerate` then manually
stripped of the known `ix_document_chunk_embedding_hnsw`/`ix_document_chunk_text_fts` false-positive
drops, per this project's standing convention):

- [ ] `add_during_market_to_announcement_time` — extend `AnnouncementTime` with `DURING_MARKET`.
- [ ] `add_finnhub_to_upcoming_earnings_date_source` — extend `UpcomingEarningsDateSource` with
      `FINNHUB`.
- [ ] `add_earnings_calendar_event_table` — new table + new `CalendarEligibilityStatus` enum.
- [ ] `add_benchmark_portfolio_table` — new table, seeded with one row ($2,000 / Moderate / Auto)
      as a data migration in the same revision.
- [ ] `add_decision_snapshot_table` — new table + new `CaptureStatus` and `DecisionSnapshotStatus`
      enums.
- [ ] *(optional, only if per-contract audit trail beyond `entry_leg_prices`/`exit_leg_prices` is
      wanted)* extend `OptionsSnapshotPurpose` with `BENCHMARK_ENTRY`/`BENCHMARK_EXIT` — no new
      table.

**Tables:**

- [ ] `earnings_calendar_event` — `id, symbol (indexed), company_id (FK company, nullable),
      company_name, logo_url, fiscal_year, fiscal_quarter, earnings_date (indexed, NOT NULL),
      session, eps_estimate, revenue_estimate, market_cap, eligibility_status, eligibility_reason,
      eligibility_checked_at, source_provider, created_at, updated_at`.
- [ ] `benchmark_portfolio` — `id, name, capital, risk_profile, expiration_mode, is_active,
      created_at`.
- [ ] `decision_snapshot` — full column set per architecture review §2.2/§2.3 (generation payload
      + entry/exit capture + P&L, all on one row).

**Indexes:**

- [ ] `earnings_calendar_event.symbol`, `.earnings_date` (both already implied indexed above —
      confirm both are real B-tree indexes, not just FK columns).
- [ ] `decision_snapshot.portfolio_id`, `.calendar_event_id`, `.company_id` (all FK, all indexed —
      every dashboard query filters by at least one of these).
- [ ] `decision_snapshot.status` — the pending/settled dashboard split (Phase 4.6) filters on this
      directly; confirm it's indexed, not just enum-typed.

**Constraints:**

- [ ] `earnings_calendar_event` unique key: `(symbol, fiscal_year, fiscal_quarter)`, with a
      `(symbol, earnings_date)` fallback path in the upsert service (Phase 4.2) for entries Finnhub
      returns without fiscal fields — **not** a second DB constraint, handled at the service layer
      per the review's reasoning (§2.1).
- [ ] `decision_snapshot` — FK constraints only (`portfolio_id`, `calendar_event_id`,
      `company_id`); **no** DB-level uniqueness on `(calendar_event_id, portfolio_id)` — "already
      frozen for this portfolio" is a service-layer check before generation (Phase 4.3), not a DB
      constraint, so a legitimate retry-after-crash isn't blocked by a hard uniqueness error.

**Done when:** all migrations apply and roll back cleanly against `edl-test-db` (port 5434,
already running and at head as of this plan), `alembic upgrade head` and `alembic downgrade -1`
(repeated per revision) both succeed, zero existing tables/columns altered.

---

## Phase 4.2 — Earnings Calendar ingestion

**Goal:** the system can answer "who reports earnings in this date range" without a human having
researched any of those companies first.

**Finnhub adapter:** already built and tested (`providers/finnhub.py`,
`FinnhubEarningsCalendarProvider`, `FinnhubCalendarEntry`/`FinnhubCompanyProfile` in
`providers/types.py`, `EarningsCalendarProvider` ABC in `providers/base.py`,
`finnhub_api_key` wired through `core/config.py` and `secret_store/environment_store.py`). No new
adapter code needed for this phase — confirmed real, confirmed passing (`test_providers_
finnhub.py`, 175 lines).

- [ ] Wire `FinnhubEarningsCalendarProvider` into `providers/factory.py` — the one concrete gap:
      no `build_earnings_calendar_provider()` exists yet. Follow the existing `_build_*` shape
      (return `None` when unconfigured, wrap in `instrument_data_provider`).

**Provider abstraction:**

- [ ] Calendar-sync service (`services/earnings_calendar_sync.py` or similar): maps Finnhub's raw
      `session` string (`"bmo"|"amc"|"dmh"|""`) to `AnnouncementTime`, upserts into
      `earnings_calendar_event` on the `(symbol, fiscal_year, fiscal_quarter)` key (fallback
      `(symbol, earnings_date)`), never creates a duplicate row for a date-changed event.
- [ ] Eligibility filter as an independent pure function (market-cap threshold, US-listed check,
      options-availability check), called after upsert — persists `eligibility_status` +
      `eligibility_reason` so a past eligibility call is never silently recomputed differently
      later. Uses `get_company_profile()` for market cap fresh from Finnhub, not the unpopulated
      `earnings_expectation_snapshot.market_cap` column the review flagged as unverified live data.
- [ ] `POST /api/v1/earnings-calendar/sync` — manual/idempotent trigger of the same sync, usable
      before the scheduler (Phase 4.2 does not require Phase 4's scheduler to exist yet — the sync
      is independently useful and testable via this endpoint first).
- [ ] `GET /api/v1/earnings-calendar` (filterable by `from`/`to`/`session`/`eligibility`),
      `GET /api/v1/earnings-calendar/{symbol}`.

**Scheduling:**

- [ ] Daily 00:00 UTC calendar-sync job — registered once the scheduler itself exists (Phase 4.3
      onward needs it too; stand it up here since this is the first job it will run). See
      architecture review §4.2 for the `AsyncIOScheduler` + `SQLAlchemyJobStore`-in-`lifespan()`
      design; not re-derived here.
- [ ] Eligibility scan job, once daily, before the trading day — reads **two** dates each run
      (today's AMC/DMH reporters, tomorrow's BMO reporters), per `compute_entry_exit_schedule()`'s
      already-built logic (`analytics/earnings_timing.py` — real, tested, not yet called from
      anywhere; this is its first caller).

**Done when:** a real Finnhub API key syncs real upcoming events into `earnings_calendar_event`
with correct upsert-not-duplicate behavior on a re-run, eligibility status is visible and
filterable via the API, and the sync job runs on schedule without touching any V3 code path.

---

## Phase 4.3 — Immutable Decision Snapshot

**Goal:** for an eligible calendar event, freeze exactly one AI decision at exactly one point in
time, reusing `generate_decision()` completely unmodified.

**Snapshot creation:**

- [ ] `freeze_decision_snapshot(db, calendar_event, portfolio)` in a new
      `services/decision_snapshot.py` — calls `resolve_auto_expiration()` (the *scored* expiration
      engine, not `generate_decision()`'s internal simpler resolver — **open question #3 from the
      architecture review must be confirmed before this line is written**, since it decides which
      code path this phase depends on), passes the result into `generate_decision(...,
      manual_expiration=...)` with `risk_profile=portfolio.risk_profile`, writes one
      `decision_snapshot` row, `status=PENDING_ENTRY`.
- [ ] Service-layer guard: "already frozen for this `(calendar_event_id, portfolio_id)`" checked
      before generation (not a DB constraint — see Phase 4.1).
- [ ] Called from the entry-capture job at `compute_entry_exit_schedule().entry_timestamp`
      (Phase 4.4 provides the actual 15:55 ET trigger; snapshot generation and entry-price capture
      happen in the same job run, back to back, so the frozen decision and its entry price are
      never separated by a gap where the market could move).

**Data-freezing rules:**

- [ ] Exactly two writer functions touch a `decision_snapshot` row for its entire life:
      `freeze_decision_snapshot()` (this phase) and `capture_exit_and_settle()` (Phase 4.5). Each
      callable at most once per row — a service-layer convention, not yet a DB guarantee (matches
      `ai_decision_version`'s existing append-only convention exactly).
- [ ] No `PUT`/`PATCH` endpoint exists for `decision_snapshot`, ever (see Phase 4.6 — read-only API
      surface).
- [ ] *(stretch goal, not blocking V1)* a Postgres `BEFORE UPDATE` trigger rejecting writes to the
      frozen generation columns — real hard guarantee instead of convention-only.

**Audit fields** (all frozen at generation time, never re-derived):

- [ ] `strategy_engine_version`, `model_version`, `prompt_version` — so a later engine change can
      never be mistaken for what an old snapshot actually saw.
- [ ] `option_chain_snapshot` (the full real chain used) and `expiration_candidates` (the full real
      `ExpirationSelectionResult`, all real alternatives, never hidden) — both JSON, both write-once.
- [ ] `generated_timestamp` — the actual wall-clock moment of generation, distinct from
      `earnings_date`/`entry_timestamp` (which is when the market-facing decision is dated to).

**Done when:** a real eligible calendar event produces exactly one frozen `decision_snapshot` row
with the full AI decision payload, a second call for the same event+portfolio is a no-op (not a
duplicate row), and `generate_decision()` itself has a zero-line diff.

---

## Phase 4.4 — Option Entry Snapshot

**Goal:** capture what the recommended trade would have actually cost, in real dollars, at the
real moment the frozen decision says it should be entered.

**IBKR integration:**

- [ ] Entry-capture job triggered at `EarningsEntryExitSchedule.entry_timestamp` (already computed
      by `compute_entry_exit_schedule()` — this phase is its first real caller).
- [ ] Uses the existing IBKR options-chain provider (`providers/ibkr_client.py` or equivalent,
      whichever `OptionsDataProvider` is configured) — no new provider code, this phase is pure
      orchestration over what already exists.
- [ ] **Honest failure handling, not optional:** if the IBKR Gateway session isn't authenticated
      (a real, previously-observed, recurring failure mode — **open question #4 from the
      architecture review must be confirmed**: is a visible `FAILED`/`SKIPPED` snapshot acceptable
      for V1, with no retry/alerting?), the job sets `entry_status=FAILED`,
      `entry_capture_error=<real message>`, and moves on to the next company — one failure never
      aborts the whole day's run, mirroring `prepare_company_research`'s existing per-step
      isolation.

**Bid/ask/mid capture:**

- [ ] `entry_leg_prices` JSON per leg: `{option_type, action, strike, bid, ask, entry_price}`.
- [ ] `entry_price` = **ASK** for a long leg, **BID** for a short leg — the conservative,
      already-specified rule (never mid, never a favorable fill assumed).
- [ ] `entry_underlying_price` captured alongside, same timestamp.

**Greeks capture:**

- [ ] `delta`/`gamma`/`theta`/`vega`, where the provider actually returns them (`OptionQuote`
      already has all four fields, `providers/types.py`) — persisted per-leg in `entry_leg_prices`,
      `None` where unavailable, never estimated or backfilled from a model.
- [ ] *(if full per-contract audit is wanted beyond the summarized leg prices)* persist the raw
      `OptionQuote` rows into the existing `options_snapshot` table, tagged
      `OptionsSnapshotPurpose.BENCHMARK_ENTRY` (Phase 4.1's optional enum extension) — no new
      table, reuses what already exists.

**Done when:** a real entry capture at a real 15:55 ET produces real bid/ask/Greeks for every leg
of the frozen strategy, a simulated IBKR-auth failure produces `FAILED` with a real error message
and never a fabricated price, and the job's per-company isolation is verified (one bad company
doesn't stop the rest).

---

## Phase 4.5 — Settlement Engine

**Goal:** once the market has had a real session to react, compute what the frozen decision
actually would have made or lost — the number this entire phase exists to produce honestly.

**Earnings outcome calculation:**

- [ ] Exit-capture job triggered at `EarningsEntryExitSchedule.exit_timestamp` (T+1 relative to
      entry per the already-resolved BMO/AMC/DMH rule — no new timing logic needed, this phase
      only calls what Phase 4.2/4.3 already trigger from).
- [ ] Real underlying reaction: reuse `PriceReaction`/`EarningsResult` exactly as
      `decision_settlement.py::find_settlement_event()` already does for `ai_decision_version` —
      same pattern, new caller, scoped to `earnings_calendar_event` instead of `EarningsEvent`.

**Option P/L calculation:**

- [ ] Exit-capture: same IBKR-integration shape as Phase 4.4, `exit_leg_prices` JSON,
      `exit_price` = **BID** for a long leg, **ASK** for a short leg (swapped from entry — same
      conservative-pricing principle, worked-out direction).
- [ ] Real, deterministic PnL math (table-driven test cases required, not spot-checked): `pnl`,
      `return_pct`, `r_multiple`, `is_win`, `capital_allocated`, `capital_utilized_pct` — computed
      once, at settlement, only from real captured entry/exit prices. **Never computed from a
      theoretical/model price when a real capture failed** — a `FAILED` entry or exit means the
      whole `decision_snapshot` stays unsettled (`status` never reaches `SETTLED`), not settled
      with a substituted estimate.

**Realized vs. expected comparison:**

- [ ] Compare the real settlement outcome against `estimated_probability`/`historical_
      compatibility`, both frozen at generation time (Phase 4.3) — this is the literal mechanism
      that finally gives this project's "True Strategy Win Rate" a non-zero N, for the first time
      (the architecture review §3 noted this is system-wide N=0 today).
- [ ] Feeds the probability-calibration dataset (predicted-bucket midpoint vs. realized rate) —
      computed on read in Phase 4.6, not stored as a separate aggregate table.

**Done when:** a settled `decision_snapshot` row shows real PnL matching a hand-computed reference
value (table-driven tests: a losing trade, a breakeven trade, a capped-max-loss trade), and
`strategy_pnl_available`-equivalent honesty is preserved — no row is ever marked `SETTLED` with a
fabricated or estimated price standing in for a real one.

---

## Phase 4.6 — Forward Testing Dashboard

**Goal:** make every decision this system has frozen, entered, and settled visible — pending and
settled alike, including the honest failures.

**Pending decisions:**

- [ ] `GET /api/v1/benchmark-portfolio/decisions?status=PENDING_ENTRY,ENTERED` — list view, real
      entry/exit status per row, `FAILED`/`SKIPPED` shown as a real, visible outcome, never hidden
      or silently filtered out.
- [ ] Detail view: read-only, reuses `DecisionTab.tsx`'s `StrategyDecisionCard`/`ProbabilityCard`
      (no risk-profile selector, no Generate button — frozen means frozen) plus
      `StrategyLabTab.tsx`'s `ExpirationSelector` table for the recommended-vs-alternatives view.

**Settled decisions:**

- [ ] `GET /api/v1/benchmark-portfolio/decisions?status=SETTLED` — same list, same detail view,
      plus the realized PnL/R-multiple/win-loss fields once populated.

**Performance metrics:**

- [ ] `GET /api/v1/benchmark-portfolio/track-record?strategy=&confidence_bucket=&dte=&risk_
      profile=&iv_regime=` — Win Rate, Average/Median R, Expectancy, Profit Factor, Max Drawdown,
      each independently verifiable against a hand-computed fixture (matching this project's
      existing Wilson-CI cross-check practice).
- [ ] `GET /api/v1/benchmark-portfolio/calibration` — predicted-bucket vs. realized-rate pairs.
- [ ] New page `pages/BenchmarkPortfolio.tsx` at `/benchmark-portfolio` — **a new page, not a tab
      on the existing `TrackRecord.tsx`** (per the architecture review §7 — this dashboard grades
      "would a real $2,000 have made money," the existing one grades "was the AI's directional call
      right"; conflating them was explicitly rejected).
- [ ] Nav: two new sidebar links (`Earnings Calendar`, `Benchmark Portfolio`) alongside the
      existing route set.

**API surface constraint carried through from the architecture review (§6):** no `POST`/`PUT`
mutation endpoint for `decision_snapshot` beyond what the scheduler calls internally — **open
question #2 must be confirmed** before this phase's router is written, since the brief's own
worked example proposed a `POST /decisions/{id}/settle` endpoint and this plan currently omits it.

**Done when:** the dashboard shows real pending and settled decisions with no fabricated numbers
anywhere, a `FAILED`/`SKIPPED` capture is visibly distinguishable from a real settled outcome, and
every metric on the page is independently reproducible from the raw `decision_snapshot` rows.

---

## Cross-phase testing note

Every phase above ends with its own passing backend unit tests before the next phase starts —
this plan does not defer testing to a final Phase 4.7. `test_providers_finnhub.py` and
`test_analytics_earnings_timing.py` are the concrete templates already in the codebase for
provider-layer and pure-logic-module tests respectively (see architecture review §8 for the full
per-module breakdown and the six-scenario Playwright E2E plan, extending the existing
`OPTIONS_PROVIDER=fixture` deterministic setup — not repeated here).
