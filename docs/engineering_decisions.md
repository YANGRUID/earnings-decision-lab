# Engineering decisions

Decisions and rejected alternatives, recorded as they're made. Grouped by phase.

## Phase 0

**Why a public GitHub repo from commit one, before any real functionality exists?**
The project's purpose is partly to demonstrate engineering process, not just a finished
artifact — commit history that shows incremental, real progress is itself part of the
deliverable.

**Why MIT license?** Simplest permissive choice for a personal portfolio project; no reason to
restrict reuse of the code (as opposed to any data, which has its own licensing constraints —
see [data_sources.md](data_sources.md)).

## Phase 1

**Why `uv` over plain `pip`/`poetry`?** Fast, single-binary dependency resolution and lockfile
management with no separate virtualenv bootstrapping step; increasingly the default choice in
current Python tooling and worth demonstrating familiarity with.

**Why SQLAlchemy 2.0's typed `Mapped`/`mapped_column` style over the legacy declarative
style?** It's the current idiomatic API (verified against current SQLAlchemy docs rather than
older cached knowledge) and gives real type-checker coverage on model attributes.

**Why Alembic migrations from the very first table, instead of `create_all()`?** Migrations
are how schema changes actually ship in a system with real data in it — starting the habit on
day one avoids ever having to reverse-engineer a migration history later.

**Why `Numeric`/`Decimal` for every financial value, never `float`?** Floats introduce binary
rounding error on decimal values like `2.85`; this bit the project's own tests during Phase 1
(`Decimal('2.85') == 2.85` is `False` in Python) before it could bite a real financial
calculation. `Numeric(18, 6)` is used uniformly for prices, EPS, revenue, and Greeks.

**Why does `earnings_result` store only reported actuals, with surprise/implied-vs-realised
computed on the fly instead of also stored as columns?** A stored derived column can go stale
if the calculation methodology changes; computing from `earnings_expectation_snapshot` +
`earnings_result` at query time means the analytics layer's documented methodology
(Phase 3/4) is always what's actually reflected. See [data_model.md](data_model.md).

**Why SEC EDGAR and Stooq specifically for Phase 1, and not a paid provider?** Both are free,
official/well-established, and require no account creation — which this project's operating
rules treat as a user action, not something to do autonomously. They cover exactly what Phase
1 needs (real filings, real actuals, real price history) without taking on a paid dependency
before the project has proven it needs one. See [data_sources.md](data_sources.md) for the
full evaluation, including providers considered and rejected for options/consensus data.

**Why does `FixtureEarningsDataProvider`/`FixtureOptionsDataProvider` exist if it's explicitly
not used in production?** The rest of the system (analytics, RAG, agents, frontend) needs to
be built and tested against a stable interface shape before a real options/consensus provider
is selected. Building against the `*Provider` ABCs with fixture data now, then swapping in a
real adapter later, is the entire point of the adapter pattern — the alternative (waiting to
build anything until a paid provider is chosen) would block unrelated work for no reason.

**Why exclude Q4 from XBRL-based earnings ingestion instead of deriving it as
`FY − (Q1+Q2+Q3)`?** That derivation is a legitimate technique, but it's an analytics
calculation with its own failure modes (a restated quarter throws off the subtraction), not
raw data. Doing it silently during ingestion would blur the line between "what the company
reported" and "what we calculated" — the project's own principle is that those must stay
distinguishable. See [limitations.md](limitations.md).

**Why leave `earnings_date` null rather than approximate it from a filing date?** Filing dates
and earnings-release dates are commonly different by 1–4 days. Writing the filing date into a
field named `earnings_date` would be a fabricated-looking fact even though the code intent
was "best guess" — better to leave it null and confirmed=`False` than have a plausible-looking
wrong value in the database.

**Why `pgvector/pgvector:pg16` as the local Postgres image starting now, when pgvector isn't
used until Phase 5?** One less migration/infrastructure change to make later; the extension
simply isn't `CREATE EXTENSION`'d until it's needed.

**Why host Postgres on port 5433, not the default 5432?** An unrelated project already runs a
Postgres container on 5432 on this machine; 5433 avoids a collision without requiring the
other project to change.

## Phase 2

**Why is there no live price-data ingestion yet, despite Stooq being implemented in Phase 1?**
Live testing found `stooq.com/robots.txt` disallows automated access and its CSV endpoint now
requires solving a JavaScript proof-of-work challenge. A fallback check of Yahoo Finance's
chart API found the identical `Disallow: /` in its robots.txt. Both are explicit "no bots"
signals from the site operators, not just inconvenient rate limits — using either would mean
scraping a site that prohibits automated access, or solving an anti-bot challenge, both of
which this project's rules rule out regardless of how common the practice is elsewhere. The
honest response is to stop, document it, and pick a provider whose terms actually allow this
(see docs/data_sources.md) rather than route around the block.

**Why use 8-K Item 2.02 filings for `earnings_date` instead of waiting for a paid
earnings-calendar provider?** SEC's submissions API tags each 8-K with its item codes, and
Item 2.02 ("Results of Operations and Financial Condition") is SEC's own designation for
results-announcement filings — this is a real, sourced signal, not a scrape or a guess. It's
free and already available from a provider already in use (SEC EDGAR), so there's no reason to
block real earnings dates on an unrelated provider decision.

**Why split `bootstrap_phase2.py` into a separate `backfill_earnings_dates.py` instead of one
script that does price ingestion too?** The SEC-EDGAR-only work (period-end dates,
earnings-release dates) has no dependency on the blocked market-data decision and is real,
useful progress on its own. Bundling it with the blocked price-ingestion code would have meant
either shipping nothing until a provider is chosen, or shipping broken/dead code that calls a
provider known not to work — neither is acceptable.

**Why Tiingo as primary and Alpha Vantage as fallback, instead of just one?** Tiingo's free
tier (500 req/hour) comfortably covers routine refreshes of six symbols; Alpha Vantage's
(25/day) doesn't, so it's a poor primary but a fine occasional backstop. Wrapping both in
`providers.fallback.MarketDataProviderChain` — try the next provider on any exception, raise
only if all fail — is a generic pattern (works for any `*Provider` ABC, not just market data)
that directly implements the project's requirement to handle provider failures without
silently returning fake data: a failure surfaces as `AllProvidersFailedError`, never as an
empty/zeroed result that looks like real data.

**Why fix `Settings`' env-file resolution now instead of leaving it?** Discovered while wiring
up the new API keys: `env_file=".env"` resolves relative to the process's current working
directory, and every ingestion script so far had been run with `cd backend`, so `.env` (at the
repo root) was silently never found — `DATABASE_URL` happened to work because the class
default matched the `.env` value by coincidence, but `SEC_EDGAR_USER_AGENT` was silently using
its placeholder default the entire time, not the real contact address configured in `.env`.
Resolving `env_file` relative to `config.py`'s own location (`Path(__file__).resolve()`)
instead of `cwd` makes this correct regardless of where a script is invoked from. The
underlying data pulled during Phase 1 is unaffected (SEC doesn't validate User-Agent content);
this was a configuration-loading bug, not a data-quality one — but it's exactly the kind of
silent-fallback-to-a-default failure mode the project's own rules warn against, so it's fixed
rather than left as a known issue.
