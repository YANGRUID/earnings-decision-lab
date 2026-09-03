# Changelog

All notable changes to Earnings Decision Lab. Dates are the real commit dates.

## v4.0.1 — 2026-09-03 — Post-release fixes

- **Earnings calendar day table.** Clicking a ticker, a day number or the "+N more" overflow on
  the Dashboard calendar opens a table of every company reporting that day (timing, market cap,
  estimates, source, V4 pipeline state and windows inside the 15:30 ET window), with each ticker
  linking to its company workspace.
- **Forward-only pipeline feed.** The Dashboard and Live Operations pipeline show the V4 era
  only: windows still open or ahead plus every event with real V4 evidence;
  `/operations/events?include_past=true` keeps the complete monitoring view.
- **Brand mark.** White-background mark as the default (README, browser tab: classic BMP
  `favicon.ico` plus PNG links, Safari-safe); sidebar shows the navy tile on the light theme and
  the white mark on the dark theme.
- **Eastern-time windows** on the Dashboard pipeline and the forward-window card; humanised
  missed-window banner.
- **Eligibility: "US listed" is a listing fact.** A foreign-domiciled company listed on a US
  exchange (Lululemon: Canada, Nasdaq) is eligible; the rule now consults SEC's own exchange
  list (`company_tickers_exchange.json`, cached daily) instead of the calendar's country field.
  A failed lookup is reported as unverified and retryable, never as "not listed".
- **Pager.** The "Show all N" control shares the quiet bordered style of the other pager buttons
  on both themes.
- **CI.** Fetches the official IBKR TWS API client before installing the backend, migrates a
  fresh database (retired-job cleanups tolerate a missing `apscheduler_jobs`), runs the backend
  tests against an isolated CI test database, and makes the tests package importable under a
  plain `pytest` invocation.

## v4.0.0 — 2026-09-03 — V4 Forward Engine

The first V4-only software release. A software-architecture release, not a proof of
profitability: the forward evidence sample is one settled-in-progress decision.

### Product
- **V4-only reset.** The V3 decision engine, its 15:55 ET benchmark pipeline, track record,
  AI Decision Journal, Cross-Company Replay, Strategy Lab, portfolio ingestion and every
  V3-only API, model, table, test, page and document were removed after a dependency audit.
  The pre-reset state is preserved on `archive/pre-v4-only-reset`; the old `main` on
  `archive/pre-v4-main`.
- **Interactive Brokers TWS API** is the market-data transport (delayed entitlement, labelled
  delayed everywhere); the Client Portal / IBeam path is documented as rollback only.
- **Research preparation automation:** ET-based nightly job with a 6-hour misfire grace, a
  13:00 ET readiness catch-up, a startup catch-up, readiness-aware re-enqueue (company row +
  AI thesis younger than 7 days) and automatic company resolution against SEC EDGAR
  (`COMPANY_RESOLUTION_FAILED` for symbols EDGAR does not list).

### Decision engine (unchanged methodology, versioned)
- DeepSeek **DecisionView** (`deepseek-v4-pro`, thinking enabled, reasoning effort high) with
  full provenance frozen on every decision; no fallback model.
- **Expected move** context, **strategy semantics**, **expected-move-aware strike geometry**,
  **T+1 scenario valuation** (core and stress grids), **ranking v1**
  (`v4-4b-t1-executable-ranking-v1`) and **six configurations** ($2K/$10K ×
  Conservative/Moderate/Aggressive) with per-configuration entry and settlement evidence.
- **15:30 ET decision** on the last trading day before the announcement and **15:30 ET
  settlement on the first post-earnings trading day** (policy
  `v4-1530-entry-1530-t1-settlement-v2`; AMC D0→D+1, BMO D−1→D0), a bounded ±5-minute
  settlement window with `SETTLEMENT_WINDOW_MISSED`, and a 15:50 ET decision deadline guard.
- **Settlement priority (this release):** one `v4_forward_window` job settles every due
  position before any new decision observation starts; the market-data lock covers only quote
  acquisition (never the DecisionView); the window is re-checked at the moment market data is
  acquired; per-attempt telemetry (`v4_forward_window_telemetry`).

### Frontend
- V4-only navigation: Dashboard, Company Search, AI Research, company workspace, V4 Decision
  Lab, Candidate Explorer, V4 Forward Track Record, Live Operations, Settings.
- Live Operations: V4 pipeline states with human labels, research readiness KPIs, job
  freshness (ON TIME / STALE / MISSED RUN), the 15:30 forward window with its execution
  priority and next-window preview, and a failure centre.
- SPA navigation performance: abandoned requests are aborted, status reads are shared, Company
  Search reads one bulk endpoint (27 s → about 1 s).
- Brand mark and favicon (`docs/brand/`).

### Safety and testing
- No brokerage order execution, no order-placement API, no position modification.
- Backend tests run only against the disposable test database and refuse live TWS sockets;
  Playwright runs on route-mocked fixtures; live QA is opt-in (`RUN_LIVE_QA=1`).
- Deterministic settlement-priority tests: same-time window, 80-second DecisionView, multiple
  settlements, failure isolation, lock cleanup, window missed, idempotency.

### Known limitations
- The forward sample is tiny; no performance is claimed.
- Market data is delayed.
- Class-share and other company-resolution edge cases may remain (`BF.A`, `BF.B`).
- Aggressive and Moderate share one strategy-family universe.

## v3.0.0 — 2026-08-20

Options Decision Engine V3 (retired in v4.0.0; see `archive/pre-v4-main`).
