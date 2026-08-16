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

### Stooq — daily OHLCV (`backend/src/providers/stooq.py`)

- **What it provides:** free daily end-of-day price history via CSV download, no key.
- **Cost:** free.
- **Auth:** none.
- **Rate limits:** undocumented publicly; the adapter retries with backoff on 429/5xx and is
  not called in a tight loop (four tickers, daily-refresh cadence).
- **Terms of service:** personal/non-commercial research use; **not** a licensed
  redistribution source. Enforced in this repo by never committing downloaded price data
  (`.gitignore` excludes `/data/` and `*.parquet`) — only derived, provenance-tagged analytics
  computed from it are persisted.
- **Reliability:** unofficial but widely used in quant/research tooling; adequate for daily
  bars on liquid large-cap names. Not relied on for anything execution-critical.
- **Status:** adapter implemented and unit-tested against fixture responses in Phase 1; live
  ingestion + a `price_bar` table land in Phase 2 (market data module).

## Evaluated, not yet wired up

These are genuine open gaps, not oversights — no options-chain or analyst-consensus data
source has both usable free access and terms compatible with the way this project stores and
re-derives data. `FixtureEarningsDataProvider` / `FixtureOptionsDataProvider`
(`backend/src/providers/fixtures.py`) exist so the rest of the system can be built and tested
against the right interface shape in the meantime — they are explicitly test-only and are
never wired into ingestion or the API (see that module's docstring).

| Provider | Covers | Cost | Notes |
|---|---|---|---|
| Alpha Vantage | consensus estimates, fundamentals | Free tier: 25 req/day | Too low a rate limit for scheduled multi-ticker snapshots; paid tier ($50+/mo) not justified for a 4-ticker personal project yet. |
| Finnhub | earnings calendar, consensus estimates, basic options | Free tier available | Candidate for `EarningsDataProvider`; not yet integrated — requires a free API key the project doesn't have configured. |
| Tradier | real-time-delayed options chains incl. Greeks | Free developer sandbox | Best-fit candidate for `OptionsDataProvider`; requires account signup, which per this project's operating rules is a user action, not something performed autonomously. |
| ORATS / CBOE DataShop | historical options chains incl. IV | Paid, priced per dataset | Only realistic source of *historical* (not just current-snapshot) options data; deferred until the project's value (or a specific need) justifies the cost. |
| yfinance (Yahoo unofficial) | current options chain, quotes | Free, no key | Rejected: relies on undocumented endpoints with unclear ToS standing for automated access — inconsistent with "never scrape sites that prohibit automated access." |

**Practical consequence:** until one of these is selected and an API key configured, the
system has no live options chain or analyst-consensus data. Historical Event Replay (Phase 4)
is scoped accordingly — it implements the architecture and runs on whatever real historical
coverage exists, and marks gaps explicitly rather than fabricating strikes, IV, or estimates
for periods with no real data. See [limitations.md](limitations.md).

## Not used

- **Earnings call transcripts:** no free, redistribution-safe source was identified.
  `TranscriptProvider` / `FixtureTranscriptProvider` exist so the RAG and extraction pipelines
  (Phases 5–6) can be built and tested against realistic shapes; wiring up a real transcript
  source (if one with acceptable terms exists) is an open item, not assumed.
