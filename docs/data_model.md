# Data model

Covers the schema landed in Phase 1 (`backend/migrations/versions/c535e22e3a6d_*`).
Later phases add tables (documented in this file as they land): `strategy_replay` (Phase 4),
document/chunk/embedding tables (Phase 5), `ai_extraction` (Phase 6), `model_evaluation`
(Phase 9).

## Entity-relationship overview

```
company (1) ──< earnings_event (1) ──< earnings_expectation_snapshot (many, point-in-time)
                       │
                       ├──1:1── earnings_result
                       ├──1:1── price_reaction
                       └──0:n── options_snapshot ──> volatility_snapshot (derived, 0:n)

company (1) ──< filing (many)
company (1) ──< options_snapshot (many)
company (1) ──< volatility_snapshot (many)
```

## Tables

### `company`
**Grain:** one row per covered ticker.
**Keys:** `id` PK; `ticker` unique; `cik` unique (SEC identifier, nullable for tickers not
yet resolved against EDGAR).
Adding a ticker is an insert here plus re-running ingestion — no schema change required,
which is the mechanism behind "V1 covers four tickers but is not architecturally limited to
four" (see [engineering_decisions.md](engineering_decisions.md)).

### `earnings_event`
**Grain:** one row per `(company, fiscal_year, fiscal_quarter)`. This is the hub every other
earnings-related table joins through.
**Keys:** `id` PK; unique `(company_id, fiscal_year, fiscal_quarter)`.
**Point-in-time note:** `earnings_date` is nullable and `date_confirmed` defaults to `False`.
It is only populated when a provider gives an actual confirmed announcement date — SEC XBRL
filing dates are deliberately **not** written here because a filing date is not the same as
the earnings-release date (see [limitations.md](limitations.md)).

### `earnings_expectation_snapshot`
**Grain:** one row per `(earnings_event, snapshot_timestamp, source_provider)` — many rows
per event, one per point-in-time pull (T-14d, T-7d, T-3d, T-1d, ... per the Phase 2 ingestion
schedule).
**Keys:** `id` PK; unique `(earnings_event_id, snapshot_timestamp, source_provider)`; indexed
on `snapshot_timestamp`.
**Point-in-time semantics (core invariant):** every column on this table must reflect only
information knowable at `snapshot_timestamp`. Rows are never updated after the fact to
"correct" a value using later information — a bad snapshot is superseded by a *new* snapshot
row, never mutated. `retrieved_at` (when we pulled it) is tracked separately from
`snapshot_timestamp` (what moment it represents) so a late/delayed pull is visible rather than
silently misdated.

### `earnings_result`
**Grain:** one row per `earnings_event` (1:1).
**Keys:** `id` PK; unique `earnings_event_id`.
Stores only *reported actuals* (`actual_eps`, `actual_revenue`, `gross_margin`, `guidance_text`,
`kpis`) with provenance (`source_provider`, `retrieved_at`, `reported_at`). Deliberately does
**not** store `eps_surprise`, `implied_vs_realised_error`, or other comparisons against
`earnings_expectation_snapshot` — those are computed by the analytics layer (Phase 3/4) from
the two tables at query time, so a re-run of the calculation always reflects the current
methodology instead of a stale cached value.

### `price_reaction`
**Grain:** one row per `earnings_event` (1:1).
**Keys:** `id` PK; unique `earnings_event_id`.
Raw observed prices (`close_price_before`, `after_hours_price`, `next_day_close`,
`five_day_close`) alongside the move percentages as reported, so the percentages can be
independently recomputed from the stored prices as a consistency check.

### `options_snapshot`
**Grain:** one row per `(company, snapshot_timestamp, expiration_date, strike, option_type,
source_provider)` — a single contract quote at a point in time. This is the raw ingestion
table; nothing here is derived.
**Keys:** `id` PK; unique on the full grain tuple above; `earnings_event_id` nullable FK
(populated when the pull was specifically scheduled around an earnings event).
**Indexing:** `company_id`, `snapshot_timestamp`, `expiration_date` are indexed — the expected
query shapes are "give me the chain for ticker X at time Y" and "give me all quotes for this
expiration."

### `volatility_snapshot`
**Grain:** one row per `(company, snapshot_timestamp, method)` — a derived aggregate computed
from `options_snapshot` rows.
**Keys:** `id` PK; unique `(company_id, snapshot_timestamp, method)`.
`inputs` (JSON) retains the raw contracts/parameters that produced the calculation, and
`method` names the documented methodology used (see
[options_methodology.md](options_methodology.md), added in Phase 3) — this is the audit trail
required by the "store method, inputs, calculated value, expiration used" requirement for
implied-move calculations.

### `price_bar` (added Phase 2)
**Grain:** one row per `(ticker, trade_date, source_provider)` daily bar. Covers both the four
tracked companies and reference series (market/sector proxies) needed for
`earnings_expectation_snapshot.sector_return` / `market_return` — `company_id` is null for
reference tickers that aren't in the `company` table.
**Keys:** `id` PK; unique `(ticker, trade_date, source_provider)`.
**Status:** live — 25,046 real daily bars across NVDA/AMD/MU/SNDK/SPY/SOXX from Tiingo,
2007-01-03 to present (SNDK from its 2025 spin-off date).

### `strategy_replay` (added Phase 4)
**Grain:** one row per `(earnings_event, strategy_name, strike_selection_rule)` — a
deterministic reconstruction of an options strategy entered before a historical earnings event.
**Keys:** `id` PK; `earnings_event_id` FK, indexed.
`legs` and `breakevens` are stored as JSON for full auditability of exactly which
strikes/premiums/rule produced the result — see
[earnings_methodology.md](earnings_methodology.md).
**Status:** table and engine (`analytics/options/replay.py`) exist and are unit-tested; **no
rows exist yet** — there is no historical options-chain data to reconstruct a real strategy
from (see [limitations.md](limitations.md)).

### `document_chunk` (added Phase 5)
**Grain:** one row per `(filing, chunk_index)` — a section-bounded, token-approximate chunk of
a filing's parsed text, with its 384-dim embedding (`BAAI/bge-small-en-v1.5` via `fastembed`)
and a PostgreSQL full-text index, for hybrid retrieval.
**Keys:** `id` PK; unique `(filing_id, chunk_index)`; `filing_id`/`company_id` FKs, indexed.
**Indexing:** HNSW index on `embedding` (cosine ops) for vector search; GIN index on
`to_tsvector('english', text)` for full-text search — see
[ai_architecture.md](ai_architecture.md) for how both are combined via Reciprocal Rank Fusion.
**Status:** live — 2,231 real chunks from 93 real SEC filings (10-K/10-Q/8-K) across
NVDA/AMD/MU/SNDK.

### `ai_extraction` (added Phase 6)
**Grain:** one row per `(filing, extraction_type, prompt_version)` — a single structured
LLM-extraction run.
**Keys:** `id` PK; `filing_id`/`company_id` FKs, indexed; `extraction_type` indexed.
`extracted_data` is the validated Pydantic schema (`schemas.extraction`), dumped to JSON.
`source_chunk_ids` retains exactly which `document_chunk` rows were given to the model —
every extracted value traces back to the text that produced it. `model` and `prompt_version`
are recorded on every row so a later prompt or model change never silently changes what an
already-stored extraction is attributed to. See [ai_architecture.md](ai_architecture.md).

### `filing`
**Grain:** one row per SEC filing, keyed by its globally-unique accession number where one
exists.
**Keys:** `id` PK; unique `accession_number`.
Source-of-record for provenance only in Phase 1 (`source_url`, `filing_date`,
`accession_number`, optionally `raw_text`). Chunking, embeddings, and structured section
parsing for RAG are separate tables added in Phase 5 — this table is not overloaded to serve
both purposes.

## No-lookahead-bias enforcement

The principle (documented in [architecture.md](architecture.md)) is enforced two ways today:

1. **Schema-level separation of concerns** — `earnings_expectation_snapshot` (what was known
   before) and `earnings_result` / `price_reaction` (what happened after) are distinct tables
   with no columns that could be silently backfilled across the boundary.
2. **Provenance on every provider-sourced row** — `source_provider` and `retrieved_at` are
   non-nullable on every table populated by ingestion, so any row can be traced back to when
   and from where it was actually pulled.

What is *not* yet enforced automatically: a database constraint or application check that
rejects a snapshot whose `retrieved_at` postdates its claimed `snapshot_timestamp` by an
unreasonable margin. That is tracked as a data-quality check to add during Phase 2's
scheduled-ingestion work, alongside the other quarantine/validation rules requested for the
system (duplicate events, invalid strikes, negative prices, etc.).
