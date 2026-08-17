# Known limitations

Honest accounting of gaps, updated as each phase lands. Nothing here is hidden in code
comments only — anything that affects what the system can honestly claim is listed here.

## Frontend (Phase 8)

- **No automated frontend test suite.** TypeScript compilation and ESLint both pass cleanly,
  and every screen was manually exercised end-to-end in a real browser against the real backend
  and real DeepSeek before this phase was considered done (Dashboard, Company, Earnings Event,
  Options Lab's calculator, AI Research's full agent flow, Historical Replay, Data/Eval
  Status) — but there's no Vitest/React Testing Library suite to catch a future regression
  automatically. A deliberate scope decision given the size of this project already, not an
  oversight; a reasonable follow-up.
- **API types are hand-mirrored, not generated.** `frontend/src/types/api.ts` must be kept in
  sync with `backend/src/schemas/api.py` manually — see
  [engineering_decisions.md](engineering_decisions.md) for why codegen wasn't set up yet.
- **The Earnings Event "centerpiece" screen has real gaps by design, not oversight:** market
  expectations (implied move, ATM IV, consensus estimates), options strategy comparison, and
  guidance previous-vs-current are all shown as honest empty/unavailable states rather than
  populated, because no options-chain or consensus-estimate provider is wired up (Phase
  1/2/7 finding) and there's no dedicated guidance-comparison REST endpoint yet (the capability
  exists as an agent tool, `compare_guidance`, reachable via AI Research, but not as its own
  page section).

## Backend API (Phase 8)

- **No authentication.** Intentional for a personal, locally-run research tool — see
  [engineering_decisions.md](engineering_decisions.md). Not suitable to expose publicly as-is.
- **Rate limiting is in-process and global**, not per-client or distributed. Fine for a single
  developer running one instance; would need a real store (Redis) behind a real auth layer
  before this could serve multiple users.
- **`httpx` is deprecated in favor of `httpx2`** (confirmed live, not assumed — Starlette's
  `TestClient` now warns on plain `httpx`). This project's entire provider layer (SEC EDGAR,
  Tiingo, Alpha Vantage, all four LLM adapters) is built on `httpx.Client` directly. A full
  migration is a real, scoped follow-up — deliberately not done reactively mid-phase without
  verifying `pytest-httpx` (used throughout this project's provider tests) actually supports
  `httpx2` yet. `httpx` remains functionally supported today, just deprecated.
- **`/api/v1/evaluations` doesn't exist yet** — deferred until Phase 9 actually has real
  evaluation results to serve; adding the endpoint first would mean either an empty stub or a
  contract that has to change once real data exists.

## Data coverage (Phase 7)

- **Single-round tool-calling, not a full multi-turn ReAct loop.** The planner can request
  multiple tools in one round, but the results aren't fed back for the model to request
  further follow-up tool calls in the same turn — a real, documented scope boundary (see
  [engineering_decisions.md](engineering_decisions.md)), not a bug. A genuine multi-turn loop
  needs `ChatMessage` to carry enough structure to reconstruct each provider's own tool-call
  history format (OpenAI-compatible vs. Anthropic differ), which isn't implemented yet.
- **Token/cost accounting excludes structured (`generate_structured`) calls** — intent
  classification and verification aren't counted in `ExecutionTrace.total_input_tokens` /
  `total_output_tokens` / `estimated_cost_usd`, because `LLMProvider.generate_structured`
  doesn't return usage metadata (unlike `generate()`). This understates true cost by a bounded,
  usually-small amount. See [ai_architecture.md](ai_architecture.md).
- **Citation marker numbering isn't globally unique if `search_filings` is called more than
  once in a single query** (uncommon, but the native planner can request the same tool twice
  with different queries). Each call's citations keep their own `[1]`, `[2]`... numbering in a
  clearly separated evidence block rather than being renumbered globally — a documented
  simplification, not a silent collision.
- **`get_options_snapshot` and `run_strategy_replay` always report no data today** — real
  behavior from real (empty) tables, not a hardcoded stub; see the Phase 1/4 entries above for
  why no options-chain data exists yet.

## Data coverage (Phase 6)

- **Numeric guidance extraction is frequently null, and that's expected.** Tested live against
  real MU 10-Q MD&A text: revenue/EPS/gross-margin/capex guidance came back `null` for both
  quarters tried. MD&A sections discuss historical results and qualitative commentary, not
  clean forward-looking numeric ranges — that typically lives in the earnings press release or
  call transcript, neither of which this project has an ingested source for yet (see
  [data_sources.md](data_sources.md)). The extraction schema is designed to return `null` when
  a value isn't actually stated rather than inventing a plausible number, so this is the schema
  working correctly, not a coverage gap in the extraction logic itself. The real gap is upstream
  (no transcript/press-release source), and is already documented as such.
- **`confidence` is intentionally left unset (`None`) on every extraction.** LLM self-reported
  confidence scores aren't reliable enough to persist as if they were meaningful signal — the
  column exists in the schema for a future validation mechanism (e.g. cross-checking against a
  second extraction, or human review), not for the model to grade its own output.

## Data coverage (Phase 5)

- **Section detection is best-effort, not guaranteed exact.** `rag/parsing.py` detects "Item N."
  headings via regex on cleaned text, not a structural parse of each filer's actual HTML
  semantics — real 10-K/10-Q/8-K filings vary enough across companies, years, and HTML
  generators that some headings could be missed or a stray line misclassified. A wrongly-labeled
  section affects only the `section` citation metadata, not which text was retrieved.
- **"Token count" is an approximation** (whitespace word count), not exact tokenization for any
  specific LLM vendor — deliberate, since this project is provider-agnostic. See
  [ai_architecture.md](ai_architecture.md).
- **No reranking model is used** — Reciprocal Rank Fusion over vector + keyword search only.
  Documented as a scale-appropriate choice for four tickers and ~2,200 chunks, not a permanent
  ceiling; see [ai_architecture.md](ai_architecture.md).
- **Local embeddings, not a hosted embedding API** — no configured LLM provider offers
  embeddings (verified: DeepSeek doesn't, OpenAI isn't configured, Anthropic points to a
  separate vendor). Quality is adequate for this project's scale but not benchmarked against a
  larger hosted model; swapping is one new `EmbeddingProvider` adapter, not a rewrite.
- **`vector_search` has no relevance floor.** It always returns the k nearest neighbors by
  cosine distance, however distant they actually are — there's no minimum-similarity cutoff.
  With ~2,200 real chunks now indexed, an off-topic query still returns *something*, just not
  necessarily anything relevant. `hybrid_search`'s RRF fusion mitigates this somewhat (a chunk
  that scores well on keyword search too gets a real boost), but a genuinely irrelevant query
  can still surface low-quality context to the LLM rather than an explicit "nothing relevant
  found." A similarity threshold is a reasonable follow-up, not yet implemented.

## Data coverage (Phase 4)

- **IV crush and event-replay engines are built and unit-tested, but have never run against
  real data.** No historical options-chain provider is wired up (see
  [data_sources.md](data_sources.md)) — the `strategy_replay` table exists (Phase 4 migration)
  and has zero rows. `analytics/earnings/iv_crush.py` and `analytics/options/replay.py` are
  tested exclusively against clearly-labeled synthetic strike/IV data. See
  [earnings_methodology.md](earnings_methodology.md) for what this means for research use
  today: the "how large is typical IV crush," "how often did the straddle underprice the
  event" questions cannot be answered with real numbers yet.

## Data coverage (Phase 2)

- **Stooq and Yahoo Finance (yfinance) cannot be used.** The Phase 1 plan was to use Stooq
  (free, no key); live testing in Phase 2 found `stooq.com/robots.txt` disallows automated
  access and its CSV endpoint now requires solving a JavaScript proof-of-work challenge. Yahoo
  Finance's chart API was checked as a fallback and found to have the identical `Disallow: /`
  in `query1.finance.yahoo.com/robots.txt`. Neither is used anywhere in this codebase — see
  [data_sources.md](data_sources.md). Replaced with Tiingo (primary) and Alpha Vantage
  (fallback), both documented authenticated APIs, keys supplied by the user and stored only in
  the gitignored `.env`. `price_bar` (25,046 real daily bars across 6 tickers, 2007–present,
  SNDK from its 2025 spin-off), `price_reaction`, and the price-derived fields on
  `earnings_expectation_snapshot` are now populated with real data for every event that has a
  confirmed `earnings_date`.
- **`earnings_date` is populated for 77 of 150 events** (NVDA 18/48, AMD 27/49, MU 28/49,
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
