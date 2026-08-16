# Known limitations

Honest accounting of gaps, updated as each phase lands. Nothing here is hidden in code
comments only — anything that affects what the system can honestly claim is listed here.

## Data coverage (Phase 2)

- **No live daily price history yet.** The Phase 1 plan was to use Stooq (free, no key); live
  testing in Phase 2 found `stooq.com/robots.txt` disallows automated access and its CSV
  endpoint now requires solving a JavaScript proof-of-work challenge. Yahoo Finance's chart API
  (the other common "free, no-key" source, used by `yfinance`) was checked as a fallback and
  found to have the same `Disallow: /` in `query1.finance.yahoo.com/robots.txt`. Neither is
  used anywhere in this codebase — see [data_sources.md](data_sources.md). `price_bar`,
  `price_reaction`, and price-derived fields on `earnings_expectation_snapshot` remain empty
  until a compliant provider (Alpha Vantage, Tiingo, Twelve Data — all documented APIs with
  usable free tiers) is selected and a free API key configured. Creating that account is a
  user action, not something performed autonomously.
- **`earnings_date` is now populated for 77 of 150 events** (NVDA 18/48, AMD 27/49, MU 28/49,
  SNDK 4/4) via 8-K Item 2.02 filings — a real, SEC-sourced signal, not a guess. The remaining
  events have no matching 8-K within the 200 most-recent-filings window SEC's API returns for
  these tickers (older 8-Ks fall outside that window); they stay `date_confirmed=False` rather
  than being assigned an approximate date.
- **The 8-K-to-quarter match is a proximity heuristic** (nearest qualifying 8-K filed 10–60
  days after the quarter's `period_end_date`), documented in
  `ingestion/earnings_date_backfill.py`. It is deterministic and unit-tested but could
  mis-assign a quarter in an unusual reporting-calendar edge case; not currently observed in
  spot checks of the 77 matches.

## Data coverage (Phase 1)

- **No discrete Q4 EPS/revenue from XBRL.** Most filers do not separately tag a standalone Q4
  figure (there is no Q4 10-Q — only a full-year 10-K). `bootstrap_phase1.py` ingests only
  Q1–Q3 quarterly actuals from SEC XBRL data; deriving Q4 as `FY − (Q1+Q2+Q3)` is a real,
  well-known technique but is analytics, not raw data, and is not yet implemented (tracked for
  a later phase). Until then, Q4 `earnings_event` rows are not created from XBRL alone.
- **`earnings_date` is unset for XBRL-sourced events.** SEC XBRL gives a filing date, not the
  actual earnings-release date (typically 1–4 days apart). Rather than write an approximation
  into a field that downstream code will treat as fact, `earnings_date` stays `NULL` with
  `date_confirmed=False` until a real earnings-calendar source is wired up (see
  [data_sources.md](data_sources.md)).
- **SNDK has only 4 quarters of history.** Sandisk re-registered as an independent SEC filer
  (CIK 0002023554) after its 2025 spin-off from Western Digital; there is no pre-spin-off XBRL
  history under this CIK. This is correct, not a bug — see
  [data_sources.md](data_sources.md) for the ticker-substitution discussion.
- **No live options chain or analyst-consensus data.** No provider has been wired up yet — see
  [data_sources.md](data_sources.md) for what was evaluated and why. `earnings_expectation_snapshot`
  fields that depend on this (implied move, IV, consensus estimates, put/call ratios) cannot
  be populated with real data until one is configured.
- **Filing full text is only fetched for one document so far** (proving the
  `FilingsProvider.get_filing_text` path works end-to-end); bulk fetch/parse/chunk for all
  filings is Phase 5's job, not Phase 1's.

## Implementation simplifications (Phase 1)

- **HTML-to-text extraction is a naive regex tag-strip**, not real document structure parsing.
  Sufficient for spot-checking that ingestion pulled the right document; real section-aware
  parsing (item boundaries, tables, exhibits) is implemented in Phase 5.
- **Tests run against the local dev database** (via a rolled-back transaction/savepoint per
  test) rather than an isolated test database or ephemeral container. Adequate for a
  single-developer project; a follow-up would be a dedicated test database or testcontainers
  in CI.
- **No automated data-quality quarantine yet** (duplicate events, invalid strikes, negative
  prices, future-dated snapshots, etc.). The schema-level unique constraints and Pydantic
  provider-boundary validation catch some classes of bad data; dedicated quarantine/warning
  handling is scoped into the Phase 2 scheduled-ingestion work.

## What this means for research use today

With only Phase 1 landed, this system can answer "what did NVDA/AMD/MU/SNDK actually report
for Q1–Q3 of a given fiscal year, and what filings back that up" — real data, fully sourced.
It cannot yet answer anything about market expectations, implied moves, or options
positioning; that requires Phases 2–4.
