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

## Phase 3

**Why derive max profit/loss/breakeven generically instead of a formula per strategy?** A
payoff-at-expiration function built from long/short calls and puts is piecewise linear, so
every extremum provably occurs at a leg's strike, at the `S=0` floor, or along the asymptotic
slope past the highest strike. Evaluating at those points generically handles all nine required
strategies (and any future one) with one algorithm, instead of nine independently-derived and
independently-tested formulas that would need to be re-verified whenever a strategy is added.
It was still cross-checked against hand-derived values for every required strategy before
trusting it, rather than assuming the general argument was implemented correctly.

**Why `Decimal` in the payoff engine but `float` in Black-Scholes?** Strikes and premiums are
exact, discrete quantities — `Decimal` is correct there, and the project's own tests caught a
`Decimal`-vs-`float` bug in Phase 1 precisely because the difference matters. Black-Scholes
inputs (implied vol, time-to-expiry as a year fraction) aren't exact quantities to begin with,
and `math.log`/`math.exp`/`statistics.NormalDist` require floats — `Decimal`'s exactness
guarantee wouldn't apply to a model output that's already an approximation.

**Why implement Black-Scholes at all, given the American/European mismatch is a real
limitation?** Documented, bounded model error from a well-understood, verifiable formula is
preferable to no Greeks at all when a provider doesn't supply them — the alternative isn't "no
error," it's "no Greeks." The mismatch is stated plainly in `docs/options_methodology.md`
rather than presented as exact, and every Black-Scholes-derived value is tagged
`GreeksSource.BLACK_SCHOLES` so it's never confused with a provider-quoted Greek.

**Why only one implied-move methodology (ATM straddle), and why say so explicitly?** It's the
standard, widely-cited approximation, but it is one of several defensible methods (wider
strangle-based estimates, variance-swap-style calculations exist too). Claiming it's the only
correct approach would be a specific, checkable claim this project doesn't want to make
incorrectly — `docs/options_methodology.md` says outright that alternatives exist.

## Phase 4

**Why build the IV-crush and event-replay engines now, when there's no historical
options-chain data to run them against?** The engines themselves (deterministic calculations,
strike-selection rules, unit tests) don't depend on which data source eventually supplies real
quotes — building and testing them now, against clearly-labeled synthetic data, means the
moment a historical options provider is selected, this code runs against real data with zero
changes. The alternative — waiting for a paid provider decision before writing any of this —
would block real, verifiable progress on a decision that has nothing to do with whether the
math is correct.

**Why is `strategy_replay` an empty table rather than skipped entirely?** The schema and engine
being real and tested, with zero rows, is a more honest state than either not building it (no
architecture to show) or populating it with invented strikes and prices to make the table look
used. `docs/earnings_methodology.md` says this directly rather than leaving it to be
discovered by inspecting row counts.

**Why is strike selection routed through one `select_strike()` entry point instead of three
separate functions?** Every replay's strike choice needs to be auditable to the same code path
regardless of which rule produced it — a reviewer (or this project's own tests) can check "was
this strike chosen before or after the event's outcome was known" by reading one function, not
three independently-written ones that could drift out of sync with each other's guarantees.

## LLM provider layer (before Phase 5)

**Why a provider-agnostic LLM layer instead of building directly against one SDK?** The
project's local development provider (DeepSeek) is not necessarily what a deployment or another
contributor would use, and none of RAG, extraction, or agent orchestration logic should need to
change if the provider does. `services/llm/LLMProvider` is the single interface those phases
depend on; `services/llm/factory.py` is the only place that reads `LLM_PROVIDER` from config.
See `docs/llm_providers.md` for the full design.

**Why implement DeepSeek, OpenAI, and a generic OpenAI-compatible provider as three separate
classes sharing one transport, instead of one class parameterized by vendor?** Config and logs
should always show which vendor is actually in use — `provider.name` is unambiguous. The
shared transport (`_OpenAICompatibleTransport`) avoids duplicating the actual wire-format
logic three times; the subclasses are thin (constructor + `name` + `capabilities`), so the
"three classes" choice costs almost nothing while keeping the public API honest about what's
configured.

**Why verify DeepSeek's API docs live instead of using existing knowledge?** Doing so caught a
real, dated fact: `deepseek-chat`/`deepseek-reasoner` were deprecated 2026-07-24, replaced by
`deepseek-v4-flash`/`deepseek-v4-pro`. Shipping the old names as a default would have meant the
project's own "verified" default was silently broken from day one — exactly the kind of claim
this project doesn't want to make without checking. The same live-verification was done for
Anthropic's Messages API shape before implementing that adapter.

**Why does Anthropic's structured-output path use a forced tool call while DeepSeek/OpenAI use
JSON mode, instead of normalizing to one mechanism?** Anthropic's Messages API has no JSON-mode
equivalent; a forced single tool call is Anthropic's own documented pattern for reliably
getting structured JSON out of the model. Pretending both mechanisms are "the same underneath"
would hide a real capability difference; `docs/llm_providers.md` states the difference plainly
instead, per the project's rule against claiming uniform feature support across providers.

**Why does does `DeepSeekProvider` disable thinking mode by default?** Found live during the
one manual connectivity check this project makes (not part of the automated suite, which never
calls a real paid API): a bare 5-max-token request to `deepseek-v4-flash` came back with empty
content and `finish_reason="length"` — the entire token budget went to hidden reasoning tokens,
because V4 models default to thinking mode on (a change from the old deepseek-chat/
deepseek-reasoner split, where non-thinking vs. thinking was chosen via model *name*). Sending
`thinking: {"type": "disabled"}` on every request (verified against
api-docs.deepseek.com/guides/thinking_mode) restores fast, deterministic responses — the
behavior structured extraction and agent tool-calling actually need, and what a caller
migrating from `deepseek-chat` would expect by default.

**Why does the OpenAI-compatible JSON-mode path skip OpenAI's stricter schema-constrained
`response_format: json_schema` mode?** That mode isn't guaranteed to exist identically on
DeepSeek or an arbitrary "OpenAI-compatible" backend. Plain JSON mode plus an embedded schema
and Pydantic validation is the subset actually portable across all three OpenAI-compatible
adapters — correct behavior on every configured provider was chosen over maximum strictness on
one specific vendor.

## Phase 5

**Why `fastembed` (ONNX) instead of `sentence-transformers` (PyTorch) for local embeddings?**
Both need no API key or account and run offline. `sentence-transformers` pulls in full PyTorch,
a much heavier dependency; the development machine was at 96% disk capacity when this decision
was made, which is a real, not hypothetical, constraint. `fastembed` gives the same
no-key/local-inference property with a fraction of the footprint (~35MB of packages vs. a
PyTorch install an order of magnitude larger).

**Why is there no hosted embedding API used at all?** Checked, not assumed: DeepSeek's official
pricing/models page lists only its two chat-completion models with no embeddings endpoint
(verified live against api-docs.deepseek.com); `OPENAI_API_KEY` isn't configured;
Anthropic doesn't offer embeddings and officially points to Voyage AI, a separate vendor whose
account this project can't create autonomously. Blocking Phase 5 on a fourth vendor decision
would have stopped real, gradable progress on a question orthogonal to whether the RAG
architecture itself is sound — `EmbeddingProvider` is abstracted the same way `LLMProvider` is,
so swapping in a hosted embedding model later is one new adapter class.

**Why regex-based "Item N." heading detection instead of a structural HTML parse of SEC's own
markup?** SEC filings are generated by many different vendors' tools across companies and
years, with meaningfully different HTML structure (table-based vs. div-based layout, inline vs.
external styles). A parser that's actually robust to that variation is a substantially larger
project than a portfolio RAG system justifies; regex heading detection on cleaned text is
honest about being best-effort (documented in docs/limitations.md), and a wrong section label
only affects citation metadata, not which text was retrieved or how accurate an answer is.

**Why Reciprocal Rank Fusion instead of a cross-encoder reranker?** RRF needs no score
normalization between vector similarity and full-text rank — two signals that aren't on a
comparable scale — and needs no additional model dependency or inference latency. At this
project's real scale (four tickers, ~2,200 chunks), a dedicated reranker isn't demonstrably
justified yet; `hybrid_search`'s interface doesn't need to change if one is added later once
retrieval quality actually demands it.

**Why does `answer_question` skip the LLM call entirely when retrieval returns nothing?**
Calling the model anyway would let it answer from its own training data with no grounding in
this project's actual filings — exactly the failure mode citations and retrieval exist to
prevent. An explicit "no matching content" result is more useful and more honest than a
plausible-sounding ungrounded answer.

## Phase 6

**Why are numeric guidance comparison and textual theme comparison two entirely separate
functions with separate prompts, rather than one combined "compare these two quarters" LLM
call?** A midpoint percentage change is exact arithmetic — delegating it to a model would trade
a guaranteed-correct calculation for a probabilistic one, for no benefit. Which commentary
*themes* are new or removed genuinely needs semantic judgment arithmetic can't provide. Keeping
them as separate code paths (`analytics.earnings.guidance_comparison` vs.
`services.extraction.compare_commentary_themes`) makes it structurally impossible for a future
change to accidentally route a numeric calculation through the LLM.

**Why does `AIExtraction` store `source_chunk_ids` and `model`/`prompt_version` on every row
instead of just the extracted result?** An extracted value is only as trustworthy as its
ability to be traced back to source text and re-verified. If a prompt is revised later,
existing rows still show exactly which prompt version produced them — nothing is silently
reinterpreted.

**A real finding worth recording:** running extraction against real MU 10-Q MD&A text returned
`null` for revenue/EPS/gross margin/capex guidance on both quarters tested. This is correct
behavior, not a failure — MD&A sections discuss historical results and qualitative commentary;
explicit forward numeric guidance for these companies typically lives in the earnings press
release or call, which this project doesn't have ingested yet (no transcript source is
available — see docs/data_sources.md). The schema's `null`-when-absent design meant this showed
up as an honest empty result instead of a plausible-looking fabricated number, which is exactly
what it's for.

## Phase 7

**Why is planning two genuinely different code paths (native tool-calling vs. a structured
planner) instead of always using the structured planner for consistency?** Native tool-calling
is the better-fitting mechanism when a provider supports it — the model can request multiple
tools in one response and the wire format is designed for exactly this. Forcing every provider
through the structured-planner path would waste a real capability the current providers all
have, purely for code-path uniformity. `provider.capabilities.supports_tool_calling` is checked
for real, not just declared: the fallback path has its own prompt, schema, and test coverage
(a scripted provider with the flag set to `False`), not a hypothetical branch nobody exercises.

**Why a single-round tool-calling loop instead of a full multi-turn ReAct-style loop (call
tools, feed results back, let the model call more tools, repeat)?** A genuine multi-turn loop
requires serializing "the assistant called these tools" back into conversation history in each
provider's own wire format — OpenAI-compatible providers expect an assistant message with
`tool_calls`, Anthropic expects preserved `tool_use` content blocks — and `ChatMessage` doesn't
currently carry enough structure to reconstruct either correctly. Given this project's actual
tool set (a handful of well-scoped, independent lookups), a single planning round that can
request multiple tools at once, followed by one separate synthesis call over the combined
evidence, covers real usage without that unshipped serialization work. Documented as a real
scope boundary, not hidden: see docs/ai_architecture.md and docs/limitations.md.

**Why is verification a separate LLM call instead of asking the synthesis call to
self-check?** A single call producing both an answer and a self-assessment of that same answer
is a weak check — the same reasoning that produced an unsupported claim is asked to notice its
own mistake. A separate call, given only the evidence and the draft (not the reasoning that
produced it), is a more independent check, closer to how a second reviewer would actually catch
an unsupported claim.

**Why bound revision to exactly one attempt instead of looping until verification passes?** An
unbounded verify-revise loop has no guaranteed termination and no cost ceiling — a model that
can't produce a fully-supported answer from the evidence would loop indefinitely (or until an
arbitrary cap) burning tokens. One bounded revision attempt, with the specific unsupported
claims fed back, is enough to fix the common case (an over-eager elaboration) while keeping
cost and latency predictable; a still-unsupported answer after one revision is returned with
`verification_supported=False` visible in the trace, not hidden.

**Why do `get_options_snapshot` and `run_strategy_replay` exist as tools at all, given they
currently always return "no data"?** They query the real tables (empty because no
options-chain provider is configured — see docs/data_sources.md), not a hardcoded message. This
means the moment a provider is wired up, these tools start returning real data with no code
change, and today they demonstrate this project's actual policy on missing data: report it
honestly through the same code path as a real result, don't omit the capability or fabricate
one.

**A bug caught before it shipped:** this phase's `ai_extraction` migration was
autogenerated, and Alembic's model-vs-database diff proposed *dropping* the HNSW and full-text
indexes added in the Phase 5 migration — because those were created via raw `op.execute()`
(pgvector/GIN indexes aren't expressible in the ORM model), so autogenerate saw them as
"undeclared" and wanted to remove them. The generated migration was reviewed before trusting it
(as every migration in this project has been, per the workflow established since Phase 1),
caught, and the erroneous drop/recreate pairs were removed from both `upgrade()` and
`downgrade()`. Because `alembic upgrade head` had already been run once with the buggy version
before the fix, the two indexes were manually recreated to match what the corrected migration
produces — verified so a fresh database and this development database end up in the identical
state.

## Phase 8 (backend)

**Why does `/options/strategies/payoff` and `/options/implied-move` accept the exact same
Pydantic models (`OptionsPayoffArgs`, `ImpliedMoveArgs`) already defined for the Phase 7 agent
tools, instead of separate API request schemas?** The shape genuinely doesn't differ between
"an LLM tool call" and "an HTTP POST body" — both are just validated structured input to the
same deterministic calculator. Defining a second, parallel schema would be duplication with no
behavioral difference, and a future field added to one would silently drift from the other.

**Why a hand-rolled in-memory sliding-window rate limiter for `/research/query` instead of a
library (e.g. `slowapi`) or a distributed store (Redis)?** This is a single-developer personal
research tool with no auth layer and no multi-instance deployment — a distributed limiter would
solve a problem this project doesn't have. The actual goal is a cost guardrail on the one
endpoint that runs several real LLM calls per request; a ~20-line sliding window over a
`deque` does that without a new dependency, and is honest about only working within one
process (documented in `api/rate_limit.py`, not left to be discovered).

**Why no authentication layer?** Per this project's own scope: it's a personal research tool
run locally, not a multi-tenant service. Adding real auth (sessions, JWTs, user accounts) would
be meaningful unshipped complexity solving a problem — protecting one user's data from other
users — that doesn't exist yet. If this were ever deployed for others to use, auth would be a
prerequisite, not an afterthought; today it would only be security theater.

**Why manually wired exception handlers (`NotFoundError`, `RateLimitedError`, `LLMError`,
generic `Exception`) instead of letting FastAPI's default error responses stand?** A caller
(this project's own future frontend, or anyone else) needs a stable, typed error shape
(`{error, request_id}`) to build real error handling against — FastAPI's default validation
error body is a different shape from an unhandled-exception traceback, which is a different
shape again. Normalizing all of them to one envelope, and logging the exception server-side by
`request_id` rather than leaking a traceback to the client, is the actual point of a typed API.

**A real finding, deliberately not acted on yet:** running the API test suite surfaced a
`StarletteDeprecationWarning` — Starlette's `TestClient` now prefers `httpx2` over `httpx` (the
library this entire project's provider layer is built on: every adapter from Phase 1's SEC
EDGAR client through Phase 7's LLM providers uses `httpx.Client` directly), and treats plain
`httpx` as deprecated as of 2026. Verified this is real (not a stale/misleading warning) via a
live search, not assumed. A full migration to `httpx2` would touch every provider adapter and
their mocked tests (`pytest-httpx`'s `httpx2` compatibility wasn't verified) — a large,
cross-cutting change with real regression risk to already-verified, real functionality across
six phases. Deliberately deferred as a tracked follow-up (see docs/limitations.md) rather than
done reactively mid-phase without full test coverage for the new library, or silently ignored.

