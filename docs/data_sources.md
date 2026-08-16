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

## Evaluated, not yet wired up

No options-chain or analyst-consensus-estimate data source has both usable free access and
terms compatible with this project's approach. `FixtureEarningsDataProvider` /
`FixtureOptionsDataProvider` (`backend/src/providers/fixtures.py`) exist so the rest of the
system can be built and tested against the right interface shape in the meantime — explicitly
test-only, never wired into ingestion or the API (see that module's docstring).

| Provider | Covers | Cost | Notes |
|---|---|---|---|
| Finnhub | earnings calendar, consensus estimates, basic options | Free tier available | Candidate for `EarningsDataProvider`; requires a free-account signup not yet done. |
| Tradier | real-time-delayed options chains incl. Greeks | Free developer sandbox | Best-fit candidate for `OptionsDataProvider`; requires signup. |
| ORATS / CBOE DataShop | historical options chains incl. IV | Paid, priced per dataset | Only realistic source of *historical* (not just current-snapshot) options data; deferred until justified by cost. |
| Twelve Data | daily OHLCV, some fundamentals | Free tier: 800 req/day | Viable third option for market data if Tiingo/Alpha Vantage both degrade; not currently needed. |

**Practical consequence:** until a consensus-estimate/options provider is selected and a key
configured, the system has no live options chain or analyst-consensus data — implied move, IV,
put/call ratios stay null on every snapshot. Historical Event Replay (Phase 4) is scoped
accordingly: it implements the architecture and runs on whatever real historical coverage
exists, marking gaps explicitly rather than fabricating strikes, IV, or estimates for periods
with no real data. See [limitations.md](limitations.md).

## Not used

- **Earnings call transcripts:** no free, redistribution-safe source was identified.
  `TranscriptProvider` / `FixtureTranscriptProvider` exist so the RAG and extraction pipelines
  (Phases 5–6) can be built and tested against realistic shapes; wiring up a real transcript
  source (if one with acceptable terms exists) is an open item, not assumed.
