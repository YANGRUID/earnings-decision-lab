# Phase 4.3 Architecture Review — Decision Freezing

Status: **review only. No code changed. No migrations created. Implementation has not
started.**

Branch: `feature/ai-earnings-forward-test`, on top of Phase 4.1 (database foundation, commit
`c5c3456`) and Phase 4.2 (earnings calendar automation, commit `254e9a6`).

This review does what it was asked to do, in order: review the eleven named components as they
actually exist in code today, then design Phase 4.3 against that reality. Two things fell out of
the review that weren't visible before actually reading the code side by side: **the
`decision_snapshot` table Phase 4.1 built cannot yet hold a complete frozen decision**, and
**V3's probability engine is deliberately never persisted, which is the opposite of what a frozen
forward-test snapshot needs**. Both are covered in detail in §2 and §8, with a concrete migration
plan — this is the review's main finding, not a footnote.

---

## Component review

**`services/decision_engine.py::generate_decision()`** ([decision_engine.py:339](backend/src/services/decision_engine.py)) — pure-computation
core, unchanged in shape since the original Phase 4 review. Gets-or-generates the thesis, resolves
the option market (live chain, either via `manual_expiration=` or the internal
`resolve_best_actionable_option_market` resolver), computes and ranks strategy candidates, attaches
why/risk bullets, and returns an in-memory `DecisionResult` — **it does not persist anything**.
Confirmed zero required changes for Phase 4.3: every parameter Phase 4.3 needs
(`risk_profile=`, `manual_expiration=`) already exists.

**`ai_decision_version`** ([ai_decision_version.py](backend/src/models/ai_decision_version.py)) — the
existing manual-decision journal. Relevant here only as the **proven template** for how a
`DecisionResult` gets turned into DB columns — `services/decision_history.py::persist_decision()`
(below) is exactly that mapping, and `freeze_decision_snapshot()` should mirror its structure
closely rather than reinvent it. `strategy_pnl` stays permanently null there by explicit design
(no real entry/exit capture pipeline) — Phase 4 exists to build that pipeline for a *different*
table, not to retrofit this one.

**`decision_snapshot`** ([decision_snapshot.py](backend/src/models/decision_snapshot.py)) — as
built in Phase 4.1: `id, ticker, company_name, strategy_direction, strategy_type,
ai_thesis_version_id, generated_at, status, created_at, updated_at`. One writer convention, `status`
is the only field expected to change post-generation. **This is materially narrower than what a
frozen AI decision actually contains** — see §2.

**`entry_snapshot`** / **`settlement_snapshot`** ([entry_snapshot.py](backend/src/models/entry_snapshot.py), [settlement_snapshot.py](backend/src/models/settlement_snapshot.py)) — Phase 4.1's append-only capture tables, FK'd to
`decision_snapshot.id` (indexed, not unique), a real Postgres `BEFORE UPDATE` trigger
(`reject_snapshot_update()`) enforcing insert-only. Out of Phase 4.3's own scope (Phase 4.4/4.5 own
these), reviewed here only to confirm the FK target (`decision_snapshot.id`) doesn't change shape
under this phase's plan — it doesn't.

**`earnings_calendar_event`** ([earnings_calendar_event.py](backend/src/models/earnings_calendar_event.py)) — Phase 4.2's Finnhub-sourced
forward calendar: `id, symbol, company_name, logo_url, earnings_date, earnings_time, eps_estimate,
revenue_estimate, market_cap, country, source, status`. `status` is `UPCOMING` for everything Phase
4.2 has ever written — `ANALYZED`/`SKIPPED`/`COMPLETED` are real enum members nothing has set yet.
Phase 4.3 is the first thing that will ever move a row off `UPCOMING`.

**Expiration engine** (`analytics/options/expiration_selection.py` + `services/expiration_engine.py::resolve_auto_expiration`)
— real, live, multi-candidate scored comparison (Event Fit/Liquidity/Quote
Coverage/Bid-Ask Quality/DTE Suitability/Data Quality), confirmed still separate from
`generate_decision()`'s own simpler internal resolver. **This is still an open question from the
original Phase 4 review, not yet resolved**: `generate_decision()`'s Auto mode does not call
`resolve_auto_expiration()` on its own. See §5.

**Risk profile** (`analytics/decision/risk_profile.py`) — `RiskProfile.{CONSERVATIVE, MODERATE,
AGGRESSIVE}`, `MIN_BID_ASK_COVERAGE`, `DEFAULT_MAX_RISK_UTILIZATION_PCT`. Confirmed: `benchmark_
portfolio` (Phase 4.1) has **no `risk_profile` column** — deliberately deferred in that phase's own
commit message ("deferred to whichever later migration actually reads them"). This is that
migration — see §8.

**Probability engine** (`analytics/decision/probability.py`, `build_estimated_probability`) —
Wilson-CI-based, built on `MoveCompatibility`. **Confirmed live, at `api/routers/research.py`'s
`_decision_response()` (around line 937-977), computed fresh on every read from the CURRENT
historical-move sample and never persisted** — the router's own comment states this is deliberate:
"so it can never go stale relative to real data that just arrived." This is the single most
important finding of this review; see §2 and §3.

**Explanation engine** (`analytics/decision/reasoning.py`) — `build_why_bullets`,
`build_risk_bullets`, `build_expiration_bullets`, `build_strike_bullets`,
`build_risk_profile_fit_bullets`, `build_why_not_alternative_bullets`. All pure functions called
once, at generation time, inside `generate_decision()` itself — already frozen into
`DecisionResult` by the time `persist_decision()` (or `freeze_decision_snapshot()`) runs. Zero
changes needed.

**Decision history** (`services/decision_history.py::persist_decision`) — confirmed exhaustively
(lines 68-140+): maps every `DecisionResult` field onto `AIDecisionVersion` columns, one field at a
time, `None`-safe throughout when `recommended` is `None`. This is the reference implementation
`freeze_decision_snapshot()` should structurally match.

---

## 1. Exact data flow

```
earnings_calendar_event (status=UPCOMING)          [Phase 4.2, done]
        |
        v
services/earnings_eligibility.py::check_eligibility()      [Phase 4.2, done -- read-only,
        |                                                    never persisted]
        v  (eligible=True)
analytics/earnings_timing.py::compute_entry_exit_schedule() [built in Phase 4 prep, never
        |                                                    called from anywhere yet]
        v  decision_generation_date / entry_timestamp (15:55 ET, BMO/AMC/DMH-correct)
        |
        v  scheduler fires AT that exact timestamp, not "whenever eligible" (see sec 3)
services/expiration_engine.py::resolve_auto_expiration()    [real, live, scored -- open
        |                                                    question, see sec 5]
        v  ExpirationSelectionResult.selected
services/decision_engine.py::generate_decision(             [reused, zero changes]
    manual_expiration=selected.expiration,
    risk_profile=portfolio.risk_profile,                    [needs sec 8's migration]
)
        v  DecisionResult (in-memory, not persisted)
        |
        +--> _historical_compatibility_for_decision-equivalent computation      [NEW logic,
        |    + build_estimated_probability()                                    frozen here,
        |                                                                       not live -- sec 2/3]
        v
freeze_decision_snapshot()  [NEW service function, Phase 4.3]
        |  writes exactly one decision_snapshot row, status=PENDING_ENTRY
        |  updates earnings_calendar_event.status -> ANALYZED
        v
decision_snapshot row, frozen forever                       [needs sec 8's migration first]
        |
        v  (Phase 4.4 -- entry capture, then Phase 4.5 -- settlement)
Future Settlement
```

Two steps above ("compute_entry_exit_schedule" and "resolve_auto_expiration") are real,
already-written code with **zero live callers today** — Phase 4.3 is the first thing that actually
invokes either of them in a real pipeline, not a hypothetical future phase.

---

## 2. What information must be frozen

**Confirmed gap: `decision_snapshot`'s current schema (Phase 4.1) cannot hold a complete decision.**
It has a decision's *header* — who, when, which direction, which thesis — but none of the
substance `generate_decision()` actually produces. Comparing `DecisionResult`'s fields (what
`persist_decision()` maps into `ai_decision_version`) against `decision_snapshot`'s current columns:

| `DecisionResult` field | Exists on `decision_snapshot` today? |
|---|---|
| `view.direction` | Yes (`strategy_direction`) |
| `recommended.ranked.candidate.category` | Yes, as a bare string (`strategy_type`) — no legs |
| `recommended.ranked.candidate.legs` | **No** |
| `recommended.ranked.candidate.analysis` (breakevens, net premium, max gain/loss) | **No** |
| `recommended.ranked.score` / `score_components` | **No** |
| `recommended.why` / `.risks` / `.why_expiration` / `.why_strikes` / `.why_risk_profile` / `.why_not_alternative` | **No** |
| `alternatives` (the #2/#3 candidates) | **No** |
| `expiration`, `underlying_price`, `implied_move_pct` | **No** |
| `risk_profile` (the actual tier used) | **No** — only the direction is captured, not which risk profile drove candidate eligibility/sizing |
| `thesis_version_id` | Yes (`ai_thesis_version_id`) |
| `provider` / `model` (engine version stamp) | **No** |
| `citations` | **No** |
| which `earnings_calendar_event` this came from | **No FK at all** |
| which `benchmark_portfolio` this belongs to | **No FK at all** |
| `earnings_date` / session this decision is for | **No** (only `generated_at`, the wall-clock moment, not the event date) |
| historical_compatibility / estimated_probability | **No** — and see below, this is new, not carried over from V3 |

**Recommendation — freeze the following onto `decision_snapshot` (migration required, see §8):**

- `calendar_event_id` (FK → `earnings_calendar_event.id`, indexed, NOT NULL) and `portfolio_id`
  (FK → `benchmark_portfolio.id`, indexed, NOT NULL) — every decision_snapshot in this design
  originates from exactly one calendar event and belongs to exactly one portfolio; without these,
  nothing downstream (settlement, the dashboard) can join back to know which earnings event a
  decision was for or size correctly against the portfolio.
- `earnings_date` (Date, NOT NULL) and `earnings_session` (reuse `EarningsTiming`) — frozen copies,
  matching the same "denormalize, don't rely on a live join" reasoning `ticker`/`company_name`
  already use. `settlement_snapshot` already independently carries its own `earnings_date` (Phase
  4.1); this makes the *scheduler* able to know which decisions are due for settlement without
  joining through `earnings_calendar_event` at all.
- `selected_expiration` (Date), `underlying_price`, `implied_move_pct`, `atm_iv` (all `Numeric`,
  matching `ai_decision_version`'s own precision) — the market snapshot the decision was made
  against.
- `risk_profile` (String, matching `ai_decision_version.risk_profile`'s existing convention) — the
  actual tier used for this generation, not re-derived from the portfolio at read time (the
  portfolio's own risk_profile could change later; this must stay what was true at generation).
- `strategy_category` (String), `legs` (JSON), `analysis` (JSON), `score` (Integer), `score_
  components` (JSON) — the recommended strategy itself.
- `why_this_strategy`, `why_this_expiration`, `why_these_strikes`, `why_risk_profile`, `why_not_
  alternative` (all JSON) — the explanation engine's output, exactly as `ai_decision_version`
  already stores its equivalents.
- `alternative_strategies` (JSON) — the #2/#3 candidates, same shape as `ai_decision_version`'s own
  field, never hidden.
- `strategy_engine_version`, `model_version`, `prompt_version` (String) — so a later engine change
  can never be mistaken for what this specific row actually saw. None of V3's tables carry this
  today; it's new, and needed specifically because this table's whole purpose is point-in-time
  reproducibility across engine changes that *will* happen over the life of this project.
- `option_chain_snapshot` (JSON, nullable) and `expiration_candidates` (JSON, nullable) — optional
  full-audit fields (the real chain used, and `ExpirationSelectionResult`'s real alternatives).
  Not strictly required for V1 functionality, but cheap to add now versus a second migration
  later, and directly serve "hedge-fund style auditability."

**New, and worth stating plainly rather than assuming it's obvious: `estimated_probability` and
`historical_compatibility` must be frozen at generation time, breaking from how `ai_decision_
version` handles them.** V3 deliberately computes these live, on every read, specifically so they
never go stale relative to newer real data. That's the right choice for a manual research journal
someone might reopen next month. It's the **wrong** choice here: the entire point of the Benchmark
Portfolio is comparing "what the AI's probability estimate said before the event" against "what
actually happened." A live-recomputed probability, read after the fact, would silently include the
just-settled event itself (and any other events that happened since) in its own historical sample
— a real, subtle form of the exact look-ahead contamination this phase exists to prevent, just
applied to the confidence number instead of the strategy pick. Recommendation: compute
`historical_compatibility`/`estimated_probability` once, during `freeze_decision_snapshot()` (the
same computation `_historical_compatibility_for_decision` + `build_estimated_probability` already
do, called at the same point in the pipeline, just written down instead of thrown away), and store
both as frozen JSON columns. This needs to be an explicit, confirmed decision (open question #1 in
the summary below) since it's a real behavioral difference from the V3 precedent this table
otherwise mirrors closely.

---

## 3. How to prevent look-ahead bias

Two independent mechanisms, both already built, neither wired to anything yet:

**Mechanism 1 — WHEN generation is allowed to run.** `analytics/earnings_timing.py::compute_entry_
exit_schedule()` is the single source of truth for this (real, tested, 160 lines of table-driven
tests, confirmed unused by any caller today). It resolves BMO/AMC/DMH/UNKNOWN sessions to a real
`decision_generation_date` + `entry_timestamp` (15:55 ET), conservatively treating anything not
confirmed AMC as BMO-shaped. **Phase 4.3's scheduler job must treat this function's output as the
only legal trigger time** — never "generate as soon as eligible" or "generate whenever the daily
job happens to run."

**Mechanism 2 — real, non-negotiable risk this review is flagging, not glossing over: `generate_
decision()` has no "as of" date parameter.** It always uses live data as of whenever it's actually
called — there is no mechanism inside `decision_engine.py` itself that prevents it from being
called *late*. If the scheduler job that's supposed to fire at exactly 15:55 ET on `decision_
generation_date` instead runs hours late (a missed cron tick, a container restart during the
window, a slow prior job blocking the executor), it will silently generate a "pre-earnings"
decision using data that has already priced in the reaction — the exact bias this whole phase
exists to prevent, and nothing in the code today would catch it.

**Recommendation:** the generation job must check its own lateness before calling `generate_
decision()`, not just fire-and-trust. Concretely: compare `datetime.now(UTC)` against the
schedule's `entry_timestamp` at the moment the job runs; if it's already past a real cutoff (e.g.
the eligible calendar event's `earnings_date`/session implies the market has already had a chance
to react — for an AMC event this means "past that day's close," for a BMO event it means "past
that day's open"), the job must record the decision_snapshot attempt as `SKIPPED` with an honest
reason, never generate one anyway with a false pre-earnings label. This is a new, small piece of
logic (not present in `compute_entry_exit_schedule()` today, which only computes the *intended*
schedule, not a runtime staleness check) — flagged as open question #2 below since it changes the
job's control flow in a way worth confirming before implementation.

**No lookahead risk in the market-data layer itself**: `OptionsDataProvider.get_option_chain`'s own
docstring already states providers "must not backfill fields using information that postdates
`as_of`" — this is an existing, enforced project-wide principle, not something Phase 4.3 needs to
add.

---

## 4. How to guarantee immutability

**`entry_snapshot`/`settlement_snapshot` — already a real guarantee, not a convention.** The
`reject_snapshot_update()` Postgres trigger (installed by migration `78ee400f83ab`) makes an
UPDATE physically fail, confirmed by a real test in Phase 4.1 (`test_entry_snapshot_rejects_
update`). Nothing changes here.

**`decision_snapshot` — currently convention only, and about to hold a lot more worth protecting.**
Today it's a small table with one mutable field (`status`) and little else worth guarding. Once §2's
migration lands, it will hold the entire substance of a decision — exactly the kind of row this
project's own stated priority ("immutable audit trail") argues should get a real, enforced
guarantee, not just a documented convention that a future bug could silently violate.
**Recommendation:** a `BEFORE UPDATE` trigger on `decision_snapshot`, narrower than `entry_
snapshot`'s — one that inspects `NEW`/`OLD` and raises only when a column *other than* `status` (and
`updated_at`, which `TimestampMixin` sets automatically) actually changes value, allowing the one
legitimate mutation (`status` rollups: `PENDING_ENTRY → ENTERED → SETTLED`/`VOID`) through. This is
new work beyond what Phase 4.1 built (that table has never needed selective-column protection
before, since it barely had columns worth protecting) — flagged as open question #3.

**Freeze discipline stays a single-writer-function convention, same as established in Phase 4.1's
decision.** `freeze_decision_snapshot()` is called exactly once per row, by whatever job the
scheduler runs at `entry_timestamp` — the same "one writer function, called once" pattern already
used for `ai_decision_version`'s generation columns and this table's own `status` field.

---

## 5. How V3 components integrate

| Component | Integration | Changes needed |
|---|---|---|
| `generate_decision()` | Called directly, with `risk_profile=portfolio.risk_profile` and `manual_expiration=` set from the Expiration Engine's pick | **Zero** |
| `resolve_auto_expiration()` | Called explicitly before `generate_decision()`, its `.selected.expiration` passed in as `manual_expiration` | **Zero**, but see open question #4: `generate_decision()`'s own Auto-mode resolver is a *different*, simpler mechanism than this scored one, and the two have never been reconciled — this phase should use the scored engine, explicitly, not `generate_decision()`'s internal default |
| `RiskProfile` / `risk_profile.py` | `portfolio.risk_profile` (new column, §8) passed straight through as `generate_decision`'s `risk_profile=` | **Zero** in the engine itself; `benchmark_portfolio` needs the column |
| `analytics/decision/probability.py` | Called once at freeze time (mirroring `_historical_compatibility_for_decision` + `build_estimated_probability`'s exact logic), result **stored**, not recomputed live | **Zero** in the probability module itself — the deviation is in *when* it's called and that the result is persisted |
| `analytics/decision/reasoning.py` | Already invoked inside `generate_decision()`; its output already sits in `DecisionResult.recommended.why*` fields by the time `freeze_decision_snapshot()` runs | **Zero** |
| `services/decision_history.py::persist_decision()` | **Not called.** `freeze_decision_snapshot()` is a new, sibling function targeting `decision_snapshot` instead of `ai_decision_version` — structurally mirrors this function's field-by-field mapping pattern rather than reusing it directly (different target schema) | New function, old pattern |
| `earnings_calendar_event.status` | `freeze_decision_snapshot()` transitions the source row `UPCOMING → ANALYZED` on success; the eligibility scan's own `SKIPPED` verdict (Phase 4.2, currently never persisted) needs a real place to land too — see open question #5 | New write path, first one this table has ever had |

---

## 6. API design

No implementation this phase (Phase 4.3 is freezing logic only), but the shape, consistent with
the already-established naming (`/earnings-calendar`, not `/earnings`) and the standing decision
against exposing any mutation endpoint for an immutable table:

```
GET /api/v1/benchmark-portfolio/decisions?status=&ticker=&from=&to=
GET /api/v1/benchmark-portfolio/decisions/{id}
```

Both read-only, mirroring `api/routers/earnings_calendar.py`'s existing pattern exactly (a plain
SQLAlchemy query, a Pydantic response schema with `from_attributes=True`). No `POST`/`PATCH`
anywhere in this router — freezing only ever happens from the scheduler job, never from an HTTP
request, for the same reason already decided for `entry_snapshot`/`settlement_snapshot`: not
exposing a write path is a stronger guarantee than exposing one and trusting callers not to misuse
it. A manual "force-freeze this event now, for testing" trigger, if wanted later, should require
the same eligibility gate the real scheduler enforces and be explicitly labeled a debug action —
not part of this phase's endpoint surface either way.

---

## 7. Testing strategy

Follows the established per-module, fixture-based pattern (`OPTIONS_PROVIDER=fixture`, no live
network, `db_session` for Postgres-backed unit tests) — concretely, for `freeze_decision_snapshot()`:

- **Field-mapping correctness**: given a fixture `DecisionResult` (constructed directly, not
  generated live), assert every column §2 adds lands with the exact right value — the same style
  as this project's existing `test_services_decision_history.py`.
- **Timing gate enforcement**: given a `compute_entry_exit_schedule()` result whose
  `entry_timestamp` is in the future relative to a fixed "now", assert the freeze function refuses
  to run (raises or returns a clear not-yet-due result) rather than generating early.
- **Lateness handling** (§3's new logic): given a "now" well past the safe window, assert the
  attempt is recorded as `SKIPPED` with an honest reason, and `generate_decision()` is never
  called at all (mock/spy it and assert zero calls) — proving the look-ahead guard actually
  short-circuits, not just that it exists.
- **Idempotency**: freezing the same `calendar_event_id` + `portfolio_id` twice is a no-op on the
  second call (mirrors the already-planned "already frozen" service-layer guard) — one test
  asserting exactly one `decision_snapshot` row exists after two calls.
- **Immutability trigger** (once §4's trigger exists): given a frozen row, assert an UPDATE to any
  non-`status` column raises, and an UPDATE to `status` alone succeeds — the same style as Phase
  4.1's `test_entry_snapshot_rejects_update`, extended to prove the *selective* column protection
  works both ways (blocks the wrong column, allows the right one).
- **Probability freezing**: given a fixture historical-move sample, assert the frozen `estimated_
  probability`/`historical_compatibility` values match a hand-computed reference (same cross-check
  discipline this project already applies to its Wilson-CI tests).
- **`earnings_calendar_event.status` transition**: assert a successful freeze moves the source row
  to `ANALYZED`, and a `SKIPPED` timing-gate outcome moves it to `SKIPPED` (not left at `UPCOMING`
  forever, and not silently advanced to `ANALYZED` on a non-generation).

No new E2E scenarios proposed in this review — Phase 4.3 has no frontend surface (§6 is read-only
API, no page consumes it yet); Playwright coverage for the Benchmark Portfolio dashboard belongs to
whichever later phase actually builds that page.

---

## 8. Migration impact

Three real migrations, all additive (no existing column altered, no existing table touched beyond
adding columns) — nothing here can break Phase 4.1/4.2's already-committed schema:

1. **`add_risk_profile_to_benchmark_portfolio`** — one column (`risk_profile`, String, matching
   `ai_decision_version.risk_profile`'s existing convention), plus the seed-row data migration this
   phase still owes from Phase 4.1 (one row: $2,000 / Moderate — the `expiration_mode` field from
   the original, broader design remains deferred; Phase 4.3 doesn't need it, since expiration
   selection here is always the scored Auto engine, not a per-portfolio toggle).
2. **`widen_decision_snapshot_for_freezing`** — every column listed in §2's recommendation:
   `calendar_event_id`/`portfolio_id` FKs (NOT NULL, indexed), `earnings_date`, `earnings_session`,
   `selected_expiration`, `underlying_price`, `implied_move_pct`, `atm_iv`, `risk_profile`,
   `strategy_category`, `legs`, `analysis`, `score`, `score_components`, five `why_*` JSON columns,
   `alternative_strategies`, `strategy_engine_version`, `model_version`, `prompt_version`,
   `estimated_probability`, `historical_compatibility`, and the two optional audit JSON columns
   (`option_chain_snapshot`, `expiration_candidates`). The table is empty in every real environment
   today (Phase 4.3 hasn't shipped, nothing has ever written a row) — this migration has no backfill
   concern.
3. **`add_decision_snapshot_immutability_trigger`** — the selective `BEFORE UPDATE` trigger from
   §4, installed the same way `78ee400f83ab` installed `reject_snapshot_update()` (raw SQL via
   `op.execute`), with its own function (or a parameterized reuse of the existing one, TBD at
   implementation time — the existing trigger function rejects *every* update unconditionally,
   which is wrong for this table's one legitimate mutable field, so this likely needs its own
   function rather than reusing `reject_snapshot_update()` as-is).

No migration touches `earnings_calendar_event`, `entry_snapshot`, or `settlement_snapshot` — their
Phase 4.1/4.2 schemas already have everything this phase needs from them (`earnings_calendar_
event.status`'s `ANALYZED`/`SKIPPED` members were already reserved and unused; `entry_snapshot`/
`settlement_snapshot`'s `decision_id` FK already points at `decision_snapshot.id` regardless of how
many columns that table gains).

---

## Summary: open questions requiring confirmation before Phase 4.3 coding starts

1. **§2 — freeze `estimated_probability`/`historical_compatibility`, breaking from V3's
   live-computation precedent.** This review recommends freezing them; confirm, since it's a
   deliberate divergence from how `ai_decision_version` handles the same numbers today.
2. **§3 — a runtime lateness check before calling `generate_decision()`.** Recommended as new
   logic, not present in `compute_entry_exit_schedule()` today. Confirm the exact cutoff rule (this
   review proposes "past the point the market could have reacted," derived from the event's own
   session) before it's implemented.
3. **§4 — a real `BEFORE UPDATE` trigger on `decision_snapshot`, selective to non-`status`
   columns.** Recommended given how much substantive data this table is about to hold. Confirm
   before migration 3 (§8) is written, since the exact trigger logic (which columns are exempt)
   needs to be right the first time.
4. **§5 — `resolve_auto_expiration()` (scored engine) vs. `generate_decision()`'s own internal
   resolver.** This review recommends the scored engine, called explicitly, with its result passed
   in via `manual_expiration=`. This is the same open question flagged in the very first Phase 4
   architecture review and still not resolved — confirm now, since Phase 4.3 is the phase that
   actually depends on the answer.
5. **§5 — where does the eligibility scan's `SKIPPED` verdict get persisted?** Phase 4.2's
   `check_eligibility()` is deliberately read-only today. Phase 4.3 needs *some* process to write
   `earnings_calendar_event.status = SKIPPED` for an ineligible event (otherwise it stays
   `UPCOMING` forever and gets re-scanned every day, harmlessly but wastefully). Confirm whether
   that write belongs inside `freeze_decision_snapshot()`'s own job (checking eligibility itself,
   immediately before attempting generation) or a separate step — this review leans toward the
   former (one job, one pass per eligible-or-not event, per day) but it's a real design choice, not
   an obvious one.
