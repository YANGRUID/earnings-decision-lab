# Data sources

Providers actually wired up, and the alternatives evaluated for the ones that aren't yet.
Updated as each phase adds or changes a provider — see
[engineering_decisions.md](engineering_decisions.md) for the reasoning behind picking one over
another.

## Wired up (Phase 1)

### SEC EDGAR — filings + actual results (`backend/src/providers/sec_edgar.py`)

- **What it provides:** filing metadata and documents (10-K/10-Q/8-K), and reported XBRL
  financial facts (actual EPS, revenue) via `data.sec.gov/api/xbrl/companyfacts`.
- **Cost:** free.
- **Auth:** none — a descriptive `User-Agent` with real contact info is required by SEC's
  fair-access policy (`SEC_EDGAR_USER_AGENT` in `.env`; the adapter refuses to run without an
  `@` in it).
- **Rate limits:** SEC asks automated callers to stay modest; this adapter enforces a minimum
  200ms between requests and retries on 429/5xx with exponential backoff.
- **Historical availability:** full — EDGAR has decades of filings for these companies.
- **Terms of service:** public data, explicitly intended for programmatic/bulk access per
  SEC's own developer documentation. No redistribution restriction on the filings themselves.
- **Reliability:** official government source; the source of truth for actual reported
  financials, which is exactly what it is used for here (not a convenience mirror).
- **Known gap:** discrete Q4 EPS/revenue is usually **not** separately XBRL-tagged (most
  filers report Q1–Q3 via 10-Q and only a full-year figure via the 10-K). Documented in
  [limitations.md](limitations.md) rather than derived speculatively in raw ingestion.

### Tiingo — daily OHLCV, primary (`backend/src/providers/tiingo.py`)

- **What it provides:** daily OHLCV, ~20 years of history per symbol in a single call.
- **Cost:** free tier — 500 requests/hour, more than enough for six symbols refreshed daily.
- **Auth:** API key (`TIINGO_API_KEY` in `.env`; free signup at tiingo.com — the user created
  the account and supplied the key, since this project doesn't create accounts autonomously).
- **Terms of service:** documented, authenticated API explicitly intended for programmatic
  use, with terms permitting personal/research use — unlike Stooq/Yahoo (below), this is not a
  scraped web page.
- **Status:** live and wired up. Backfilled all four tickers plus SPY/SOXX reference series
  back to 2007-01-03 (SNDK from its 2025 spin-off date) — see
  [data_model.md](data_model.md#price_bar-added-phase-2).

### Alpha Vantage — daily OHLCV, fallback (`backend/src/providers/alpha_vantage.py`)

- **What it provides:** same shape as Tiingo (`TIME_SERIES_DAILY`, `outputsize=full`).
- **Cost:** free tier — 25 requests/day, ~5/minute. Too tight to be primary for six symbols'
  routine refreshes, but a reasonable backstop for the rare case Tiingo is unavailable.
- **Auth:** API key (`ALPHA_VANTAGE_API_KEY` in `.env`).
- **Status:** live, wired up as the fallback leg of
  `providers.fallback.MarketDataProviderChain` — used only if Tiingo raises.

### Rejected: Stooq and Yahoo Finance (yfinance) — discovered live in Phase 2

The original Phase 1 plan was Stooq (free, no key). Live testing in Phase 2 found:
1. `stooq.com/robots.txt` sets `User-agent: * / Disallow: /` (Googlebot/Bingbot excepted) —
   Stooq's own policy disallows general automated access.
2. Its CSV download endpoint now serves a JavaScript proof-of-work challenge instead of data.

A fallback check of Yahoo Finance's chart API (what `yfinance` uses under the hood) found the
identical `Disallow: /` in `query1.finance.yahoo.com/robots.txt`. Both are unambiguous
"no bots" signals — and both are extremely common "free, no-key" sources in quant/research
tutorials despite this. Per this project's rule against scraping sites that prohibit automated
access (and against bypassing bot-detection), neither is called anywhere in this codebase; see
`backend/src/providers/stooq.py`'s module docstring for the retained-but-unused adapter.
SEC-EDGAR-only pieces (actuals, 8-K-sourced earnings dates) were never affected by this, since
`ingestion/backfill_earnings_dates.py` has no market-data dependency.

### Alpha Vantage — analyst consensus estimates (`backend/src/providers/alpha_vantage_estimates.py`, Phase 12)

- **What it provides:** `EARNINGS_ESTIMATES` (consensus EPS/revenue, high/low, analyst count,
  30-day EPS revision counts, per fiscal period) and `EARNINGS_CALENDAR` (the provider's own
  next-report-date prediction). Both confirmed **live, on the free tier** during Phase 12.
- **Cost:** free tier, same shared 25 requests/day / ~5/minute budget as the OHLCV adapter
  above — both endpoints count against the same daily quota.
- **Status:** live and wired up. See [engineering_decisions.md](engineering_decisions.md)
  (Phase 12) for why `EarningsEstimateSnapshot` is keyed by `fiscal_period_end_date` rather
  than `(fiscal_year, fiscal_quarter)`, and for a real matching bug (fiscal quarter vs. fiscal
  year sharing one period-end date) caught during Phase 12 live verification.
- **Data-quality caveat, observed live:** Micron's (MU) real `EARNINGS_ESTIMATES` response as of
  2026-08-17 has forward (2026+) consensus EPS/revenue figures far above its own recent
  historical range in the same response (e.g. "fiscal quarter" EPS jumping from ~$2-4 in
  2025 quarters to $31+ from Q1 FY2026 onward; "fiscal year" EPS of $73-155). This is Alpha
  Vantage's own third-party consensus data, faithfully parsed and stored as-is (not a parsing
  bug in this project) — treat MU's specific forward estimates with real skepticism until
  cross-checked against another source, rather than assuming this project's numbers are wrong.
  NVDA's real forward estimate (EPS ~$2.08 for the quarter ending 2026-07-31) looked plausible
  by comparison.

### Alpha Vantage — options chain (`backend/src/providers/alpha_vantage_options.py`, Phase 12)

- **What it provides (on paper):** `REALTIME_OPTIONS` (current chain incl. Greeks with
  `require_greeks=true`) and `HISTORICAL_OPTIONS` (past chains).
- **Confirmed live, Phase 12: both require a premium Alpha Vantage subscription this project's
  key doesn't have.**
  - `REALTIME_OPTIONS` returns HTTP 200 with an explicit
    `"message": "This is a premium endpoint..."` field and artificial sample data (fake
    contract IDs, an invalid `"2099-99-99"` expiration) — the adapter detects this shape and
    raises `PremiumEndpointRequiredError` rather than ever parsing it as real data.
  - `HISTORICAL_OPTIONS` returns an even more explicit
    `{"Information": "... This is a premium endpoint ..."}` body with no data at all — checked
    live again in Phase 12 (see below); the exact captured response is in `backend/tests/test_providers_alpha_vantage_options.py`.
- **Practical consequence:** as an Alpha Vantage data source, `OptionsSnapshot` stays empty via
  this provider, and every downstream calculation that depends on it (implied move, ATM IV, IV
  term structure, put/call ratios) correctly returns null rather than a fabricated value. The
  parsing path for a real (non-demo) response is implemented and unit-tested against Alpha
  Vantage's documented schema, ready to verify the moment a subscription exists. **As of Phase
  13, this is no longer the only path to real options data** — see the Interactive Brokers
  section below, which does return real data today.

### Interactive Brokers — options chain (Phase 13 via the Client Portal Gateway; TWS API since Phase 3 of the TWS migration)

> **Active transport today: the TWS socket API** (`IBKR_PROVIDER=tws`, see
> [ibkr_architecture.md](ibkr_architecture.md)). The Client Portal path below is the original
> integration, kept only as a manual rollback; the portfolio endpoints it mentions were removed
> with the V3 product.

- **What it provides:** real options-chain data (bid/ask/last/volume/Greeks/IV) and real,
  read-only portfolio positions, sourced from the user's own IBKR account via the local Client
  Portal Gateway they run and authenticate themselves — see
  [ibkr_integration.md](ibkr_integration.md) for the full architecture, endpoint citations, and
  real verification record.
- **Confirmed live, Phase 13:** real, on the free/base account tier (no premium subscription
  required for the endpoints used) — this is the first provider in this project that returns
  genuine options-chain data. Verified end to end for NVDA: 22 real option contracts discovered
  and fetched, `OptionsSnapshot` rows went from 0 to 22, `VolatilitySnapshot` rows went from 0 to
  1 with real ATM IV (46.3%), real implied move (6.41%, $14.43), and real put/call ratios — all
  visible on the Earnings Event page with zero frontend changes, since Phase 12 already built the
  real display logic.
- **Real, observed entitlement nuance:** the underlying stock quote came back `delayed`; option
  quotes came back `frozen` (checked ~2 minutes after the regular session closed) — plausibly a
  real options-data-subscription gap, or simply reflecting the market being closed at the moment
  of the check; not fully disambiguated (would need a check during live market hours), and
  reported as observed rather than assumed either way.
- **Bounded fetch, not a full chain:** the Gateway has no bulk "return the whole chain" endpoint
  like Alpha Vantage's `REALTIME_OPTIONS`, so `IBKROptionsProvider` fetches a deliberately bounded
  window (nearest expiration after the real earnings date being researched, 5 strikes either side
  of ATM) rather than walking every listed month and strike.
- **Read-only, local-only:** no order-execution endpoint is ever called; the Gateway is never
  assumed to run anywhere but the user's own machine (see `docs/ibkr_integration.md`'s
  "local-first" section) — a future Azure deployment is an explicitly separate, deferred decision.

### Historical move statistics and implied-vs-realized moves

With `HISTORICAL_OPTIONS` confirmed premium-gated, no historical options-chain reconstruction
exists. Two things that **are** real remain in the product:

1. **Historical price-move statistics** per company (average/median/largest absolute
   next-day move, with the largest move's real direction) computed from already-ingested,
   real `PriceReaction` rows — `backend/src/analytics/earnings/historical_moves.py`, shown in
   the company workspace's Earnings Setup tab.
2. **Forward `VolatilitySnapshot` rows** computed ahead of real earnings dates from persisted
   options snapshots; the V4 forward test uses the same expected-move evidence at decision time.

The Cross-Company Replay screen and `GET /api/v1/replay` were removed in the V4-only reset
(2026-09-02).

### Rejected/deferred alternatives for options-chain data

| Provider | Covers | Cost | Notes |
|---|---|---|---|
| Tradier | real-time-delayed options chains incl. Greeks | Free developer sandbox | Candidate `OptionsDataProvider` if Alpha Vantage's options endpoints stay out of reach; requires signup, not yet done. |
| ORATS / CBOE DataShop | historical options chains incl. IV | Paid, priced per dataset | Only realistic source of genuinely *historical* (not just current-snapshot) options data; deferred until justified by cost. |
| Twelve Data | daily OHLCV, some fundamentals | Free tier: 800 req/day | Viable third option for market data if Tiingo/Alpha Vantage both degrade; not currently needed. |

`FixtureEarningsDataProvider` / `FixtureOptionsDataProvider` (`backend/src/providers/fixtures.py`)
remain test-only fixtures for exercising the interface shape — never wired into ingestion or
the API.

### EarningsAPI.com (primary) / Finnhub (fallback) — the forward-looking earnings calendar

Originally Finnhub alone (Phase 4): rejected as redundant with Alpha Vantage through Phase 12, then
reversed for one specific use case the forward test needs — a real cross-symbol "who reports in this date range" calendar scan, which nothing in
this codebase's existing per-ticker providers can answer. Finnhub's free tier was later confirmed
live, against this project's own real data, to return far-future placeholder dates (clustering
around May–June 2027, even for mega-caps with well-known real quarterly cadences) once its own
near-term coverage ran out — the sync never failed, it just silently stored dates that didn't
reflect reality. See `EARNINGS_CALENDAR_PROVIDER_ARCHITECTURE_REVIEW.md` for the full investigation.

`providers/earningsapi.py::EarningsApiCalendarProvider` is now primary: wraps
`/v1/calendar/earnings?date=` (one real calendar date per call, no range parameter) and
`/v1/profile/{symbol}` (name/exchange/country/market cap). Free tier: 100 req/day, 1,000/month —
`services/earnings_calendar_sync.py`'s own per-date dedup (skip any date already covered by an
existing row) keeps real usage to roughly 1–3 requests/day in steady state. `providers/finnhub.py::
FinnhubEarningsCalendarProvider` is now fallback, used only if EarningsAPI.com is unreachable or
unconfigured — see `providers/fallback.py::EarningsCalendarProviderChain`. Both are wired through
the same, pre-existing `providers/base.py::EarningsCalendarProvider` interface — no second, parallel
abstraction was introduced. Deliberately does not touch Alpha Vantage anywhere — the existing
per-ticker "next earnings date" flow (`services/market_expectations.py`) is untouched and keeps using
`AlphaVantageEarningsEstimatesProvider`.

## Not used

- **Earnings call transcripts:** no free, redistribution-safe source was identified.
  `TranscriptProvider` / `FixtureTranscriptProvider` exist so the RAG and extraction pipelines
  (Phases 5–6) can be built and tested against realistic shapes; wiring up a real transcript
  source (if one with acceptable terms exists) is an open item, not assumed.
