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
