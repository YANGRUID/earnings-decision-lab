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

### Stooq — daily OHLCV — **NOT USABLE, discovered live in Phase 2**

- **Status:** implemented and unit-tested in Phase 1 against fixture responses, with the
  intent to use it for live daily OHLCV in Phase 2. When actually run against the live
  endpoint, two things were found:
  1. `stooq.com/robots.txt` sets `User-agent: * / Disallow: /` (an explicit allowlist for
     Googlebot/Bingbot only) — i.e. Stooq's own policy disallows general automated access.
  2. The CSV download endpoint (`stooq.com/q/d/l/`) now returns a JavaScript proof-of-work
     challenge page instead of data, rather than a 200 with a CSV body.
  Both are unambiguous "do not automate against this" signals. Per this project's operating
  rule ("never scrape sites that prohibit automated access") and its rule against bypassing
  bot-detection, the adapter is **not called against the live endpoint anywhere in this
  codebase** — see `backend/src/providers/stooq.py`'s module docstring. This is a genuine,
  live-discovered gap, not a design choice made in advance.
- **Why this is worth stating plainly:** Stooq (and, per the same check, Yahoo Finance's
  `query1.finance.yahoo.com` chart API — also `Disallow: /`) are both extremely common
  "free, no-key" data sources in quant/research tutorials and tools. Neither is actually
  compliant with its own robots.txt for automated use. This project follows the stated
  policy rather than the common practice.
- **What still works despite this:** SEC-EDGAR-only pieces (real earnings actuals, real
  earnings dates via 8-K Item 2.02 — see below) are unaffected, since they never depended on
  Stooq. `backend/src/ingestion/backfill_earnings_dates.py` runs independently of any
  market-data provider.
- **Replacement:** an open decision — see docs/limitations.md. The realistic candidates
  (Alpha Vantage, Tiingo, Twelve Data, Finnhub, Polygon) all have documented APIs with usable
  free tiers, but every one of them requires a free-account signup this project cannot
  perform autonomously (creating accounts is outside this project's authorized actions).

## Evaluated, not yet wired up

These are genuine open gaps, not oversights — no options-chain or analyst-consensus data
source has both usable free access and terms compatible with the way this project stores and
re-derives data. `FixtureEarningsDataProvider` / `FixtureOptionsDataProvider`
(`backend/src/providers/fixtures.py`) exist so the rest of the system can be built and tested
against the right interface shape in the meantime — they are explicitly test-only and are
never wired into ingestion or the API (see that module's docstring).

| Provider | Covers | Cost | Notes |
|---|---|---|---|
| Alpha Vantage | daily OHLCV, consensus estimates, fundamentals | Free tier: 25 req/day, 5/min | `TIME_SERIES_DAILY` with `outputsize=full` returns ~20y in one call — 6 calls (4 tickers + SPY + SOXX) fits the daily quota easily. Strongest current candidate for `MarketDataProvider`. Documented public API, not a scrape. |
| Tiingo | daily OHLCV (20+ years), fundamentals | Free tier: generous (500/hr) | Documented API with an explicit ToS permitting personal/research/academic use. Strong alternative/complement to Alpha Vantage. |
| Twelve Data | daily OHLCV, some fundamentals | Free tier: 800 req/day | Documented API; viable alternative. |
| Finnhub | earnings calendar, consensus estimates, basic options | Free tier available | Candidate for `EarningsDataProvider`; not yet integrated. |
| Tradier | real-time-delayed options chains incl. Greeks | Free developer sandbox | Best-fit candidate for `OptionsDataProvider`. |
| ORATS / CBOE DataShop | historical options chains incl. IV | Paid, priced per dataset | Only realistic source of *historical* (not just current-snapshot) options data; deferred until the project's value (or a specific need) justifies the cost. |
| yfinance (Yahoo unofficial) | quotes, options chain, daily OHLCV | Free, no key | Rejected: `query1.finance.yahoo.com/robots.txt` sets `Disallow: /` — confirmed live during Phase 2, same as Stooq. Not used for any purpose. |

**All of the API-key candidates above require a free-account signup** — an action this
project cannot perform autonomously (see operating rules). Until the user creates an account
with one of these and supplies the key via `.env`, the system has no live daily price history
beyond what was already backfilled in Phase 1 (SEC EDGAR actuals), and no live options chain
or analyst-consensus data. Historical Event Replay (Phase 4) is scoped accordingly — it
implements the architecture and runs on whatever real historical coverage exists, marking gaps
explicitly rather than fabricating strikes, IV, prices, or estimates for periods with no real
data. See [limitations.md](limitations.md).

## Not used

- **Earnings call transcripts:** no free, redistribution-safe source was identified.
  `TranscriptProvider` / `FixtureTranscriptProvider` exist so the RAG and extraction pipelines
  (Phases 5–6) can be built and tested against realistic shapes; wiring up a real transcript
  source (if one with acceptable terms exists) is an open item, not assumed.
