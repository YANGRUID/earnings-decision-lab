# Phase 4.6 Architecture Review — Track Record & Performance Analytics

Status: **implemented.** See the final report delivered alongside this commit for the exact
files, APIs, metrics, and tests.

Branch: `feature/ai-earnings-forward-test`.

## Addendum (2026-08-21) — architecture decisions confirmed, superseding §3/§4/§5/§7 in part

The open questions in §7 were resolved by explicit direction. Kept alongside the original
recommendation for its reasoning, matching this project's existing convention for a
reviewed-and-confirmed-or-reversed call (see `PHASE4_ARCHITECTURE_REVIEW.md`'s §2.3 addendum and
`PHASE4_5_SETTLEMENT_ARCHITECTURE_REVIEW.md`'s own).

1. **§7.1, Max Drawdown basis — confirmed, as recommended.** Real equity curve from the $2,000
   `BenchmarkPortfolio.initial_capital`, settled decisions ordered by `SettlementCaptureAttempt.
   captured_at`, running peak, drawdown $ and drawdown % both computed. Explicitly **not**
   R-multiple-based — confirmed by direct instruction, matching this document's own original
   reasoning (a dollar equity curve against the real fixed $2,000 benchmark is the literal,
   unambiguous "did the AI benchmark portfolio's real balance ever dip" question; an R-multiple
   curve answers a different, risk-normalized question this phase isn't asked for).
2. **§7.2, "no settled trades" behavior — confirmed and clarified.** The API layer represents
   this via `settled_decisions: 0` plus every metric `null`/an empty `Rate` — exactly as this
   document recommended, never a literal string in the JSON response. The literal message *"No
   settled trades available"* is a **frontend** concern: the Track Record page renders that text
   when it sees `settled_decisions == 0`, rather than attempting to render a chart or a `0%` badge
   over an empty sample. Both halves of the requirement (`null` in the API, the honest string in
   the UI) are satisfied by two different, correctly-scoped layers, not one field trying to be
   both.
3. **§5, DTE bucket boundaries — overridden, and the reference date is different from what this
   document proposed.** Not `generated_at.date()` (decision generation date) — **`DecisionSnapshot.
   selected_expiration − earnings_calendar_event.earnings_date`** (days from the earnings event
   itself to expiration, not days from decision generation to expiration.) Buckets: `0-3`, `4-7`,
   `8-14`, `15-30`, `30+` (five buckets, not the four this document proposed). Implemented as
   contiguous, non-overlapping integer ranges (`0-3`, `4-7`, `8-14`, `15-30`, `31+` internally,
   labeled `"30+"` per the literal instruction) — the boundary at exactly 30 is not itself
   ambiguous in the brief's own list once read as contiguous buckets (each bucket's lower bound is
   the previous bucket's upper bound + 1), so `30+` means DTE ≥ 31 while keeping the `"30+"` label
   as given.
4. **§1, data source — confirmed, as recommended.** Only `SettlementCaptureAttempt` +
   `EntrySnapshot` + `ExitSnapshot` + `DecisionSnapshot` (and, transitively, `EntryCaptureAttempt`
   and `BenchmarkPortfolio`/`VolatilitySnapshot` for the joins §2 already documented). The legacy
   `AIDecisionVersion`/`services/track_record.py` system is never imported.
5. **§4, probability calibration buckets — overridden.** Five buckets, not four: `<60%`, `60-70%`,
   `70-80%`, `80-90%`, `90%+` — this document's §7.4 open question (whether to add a `<60%`
   bucket) is resolved in favor of including it. Implemented as half-open intervals with no gap
   and no overlap (`p < 60`, `60 ≤ p < 70`, `70 ≤ p < 80`, `80 ≤ p < 90`, `p ≥ 90`, where `p =
   estimated_probability × 100`) — the same non-overlap reasoning as the DTE buckets above. The
   "confidence bucket" filter used for strategy breakdowns (§5) reuses these exact same five
   buckets and boundaries, per this document's own §2 observation that the two concepts are the
   same underlying number.
6. **§6, migration — confirmed, as recommended.** None. Every field this phase needs already
   exists on already-migrated tables; this phase is pure read-side aggregation, no new table, no
   cache row ever written.
7. **§7.6, frontend — overridden.** A small Track Record page is in scope after all, reusing
   existing dashboard architecture (`Layout`, the existing route-registration convention, existing
   API-client/type conventions) — **explicitly not a redesign**, and explicitly a **new** page
   (matching this document's own §1 observation, inherited from `PHASE4_ARCHITECTURE_REVIEW.md`
   §7, that this grades a different thing than the existing `TrackRecord.tsx` and must not become
   a tab bolted onto it). Scope, precisely: a benchmark summary panel, the performance metrics
   (§3.1), calibration (§4), and the strategy breakdown (§5) — no redesign of any existing page or
   component, no new chart library, no new design system.

**Explicit scope confirmation carried forward unchanged from the original document**: read-only
aggregation and (now) a small, additive read-only frontend page — never a mutation endpoint, never
a fabricated metric, never a percentage computed from a zero sample.

Scope of this document: designs the read-side analytics layer that turns the four immutable
tables Phase 4.1–4.5 already produce — `DecisionSnapshot` + `EntrySnapshot` +
`EntryCaptureAttempt` + `SettlementCaptureAttempt`/`ExitSnapshot` — into **Verified AI
Forward-Test Performance Analytics**: portfolio-level performance, prediction accuracy,
probability calibration, and strategy/confidence/DTE/risk-profile/IV-regime breakdowns. This
phase computes and serves that analysis; it does not build a dashboard, does not touch track
record for the *other* (V3, `AIDecisionVersion`-based) system, and does not touch any prior
Phase 4 table's schema or writer.

---

## 0. Pre-flight verification

Confirmed directly (`git branch`, `git log`, `pytest`), not assumed:

| Check | Result |
|---|---|
| Current branch | `feature/ai-earnings-forward-test` ✓ |
| Phase 4.1 (`c5c3456`) | Present — `decision_snapshot`, `entry_snapshot`, `settlement_snapshot`, `benchmark_portfolio`. |
| Phase 4.2 (`254e9a6`) | Present — Finnhub calendar sync, eligibility, scheduler skeleton, read API. |
| Phase 4.3 (`805517a`) | Present — decision freezing, full `decision_snapshot` immutability. |
| Phase 4.4 (`8286c84`, hardened `d1b9ae3`) | Present — `entry_capture_attempt`/`entry_snapshot`, live underlying context, two-sided entry window. |
| Phase 4.5 (`2a9192a`) | Present — `settlement_capture_attempt`/`exit_snapshot`, exit capture, real realized P&L/return/R-multiple, `GET /settlements/{decision_id}`. |
| Test suite | 1105 passed, 0 failed, as of `2a9192a`. |

Working tree clean (only the pre-existing, unrelated `.claude/` untracked directory). **No prior
phase will be modified by this phase** — this document proposes new, additive modules only (a
new analytics module, a new service module, new schemas, a new router), reading the existing
tables exactly as they stand.

---

## 1. What already exists that this phase must not duplicate

**A parallel system already computes something similarly named, for a different table.**
`services/track_record.py` (Phase 14.9 Part H) computes Directional Accuracy / Breakeven Success
/ confidence calibration — but over `AIDecisionVersion`, V3's manually-triggered single-ticker
decision journal, not `DecisionSnapshot`. Its own module docstring is explicit about why it can
never compute a real win rate: *"Strategy Win Rate: ... ONLY computable when real point-in-time
option entry/exit prices exist, which this project does not yet capture ... so this metric is
always 'not available' today."* **That is exactly the gap Phase 4.1–4.5 exist to close** — this
project now has real, captured, point-in-time entry and exit option prices, for the first time.
Phase 4.6 is not a modification of `track_record.py`; it is the module V3's own code has been
waiting for, over a different, real-money-shaped table.

**The shape of `Rate`/`ConfidenceBucket` is a proven, directly reusable pattern — not the same
classes.** `services/track_record.py`'s `Rate` dataclass (`correct`/`total`, `.pct` property that
returns `None` when `total == 0`) is precisely the *"never fabricate 67% from zero samples"*
mechanism this phase's own constraint requires. `schemas/api.py`'s `RateResponse`/
`ConfidenceBucketResponse` are the exact wire-shape template. Phase 4.6 defines its own
equivalents (bound to Phase 4 tables, not `AIDecisionVersion`) rather than importing V3's —
different source table, different query, same shape and same honesty discipline.

**What the API prefix precedent actually is, today (not what the original plan sketched).**
`PHASE4_ARCHITECTURE_REVIEW.md`/`ARCHITECTURE_REVIEW_PHASE4.md` (written before Phase 4.4 shipped
any code) sketched `GET /benchmark-portfolio/track-record` and `GET /benchmark-portfolio/
calibration`. What was actually built in Phase 4.4/4.5 is a `/benchmark` prefix
(`api/routers/benchmark_entries.py`, `GET /api/v1/benchmark/entries`) and a standalone
`/settlements/{decision_id}` (`api/routers/settlements.py`). **This phase follows the prefix that
was actually built**, not the one that was planned before it existed: `GET /api/v1/benchmark/
track-record` and `GET /api/v1/benchmark/calibration` (§5).

---

## 2. Data availability audit — verified column-by-column against the real, current schema

Every metric below was checked against the actual model files (not assumed from memory), for one
reason: this phase's own constraint is "never fabricate," and the fastest way to violate that
unintentionally is to assume a column exists that doesn't.

| Need | Source | Confirmed |
|---|---|---|
| Which decisions are settled, and their outcome | `SettlementCaptureAttempt.status`, `.realized_pnl`, `.return_pct`, `.r_multiple`, `.is_win` | ✓ all real typed columns, Phase 4.5. |
| Portfolio scoping | `SettlementCaptureAttempt.benchmark_portfolio_id`, `DecisionSnapshot.benchmark_portfolio_id` | ✓ |
| Entry/exit underlying prices (for directional & range accuracy) | `EntryCaptureAttempt.underlying_price`, `SettlementCaptureAttempt.underlying_price` | ✓ both real, live-captured (Phase 4.4 hardening / Phase 4.5). |
| AI's directional call | `DecisionSnapshot.strategy_direction` (`DecisionDirection`: `strong_bullish`/`bullish`/`neutral`/`bearish`/`strong_bearish`) | ✓ reuses V3's own enum directly. |
| AI's predicted probability | `DecisionSnapshot.estimated_probability` | ✓ frozen at generation (Phase 4.3 decision #2). |
| Strategy type (breakdown axis) | `DecisionSnapshot.strategy_type` (free string, e.g. `"iron_condor"`) | ✓ |
| Risk profile (breakdown axis) | `BenchmarkPortfolio.risk_profile` via `DecisionSnapshot.benchmark_portfolio_id` join — **not** duplicated onto `DecisionSnapshot` itself | ✓ confirmed no redundant column; a join is required, and is fine (today there is exactly one portfolio, so this is a single-row join, not a real cost). |
| IV regime (breakdown axis) | `DecisionSnapshot.volatility_regime` (`"high"`/`"normal"`/`"low"`/`"unknown"`, from `iv_percentile` at generation, Phase 4.3) | ✓ |
| DTE (breakdown axis) | `(DecisionSnapshot.selected_expiration - DecisionSnapshot.generated_at.date()).days` | ✓ same formula `analytics/options/expiration_selection.py` already uses (`dte = (expiration - reference_date).days`) — reused, not reinvented. |
| Confidence bucket (breakdown axis + calibration axis) | `DecisionSnapshot.estimated_probability`, bucketed | ✓ same field as calibration; "confidence bucket" and "probability calibration axis" are the same underlying number, bucketed the same way — see §4. |
| Sizing/risk unit for R-multiple, already computed, never re-derived | `EntryCaptureAttempt.initial_max_risk`, `.net_entry_cash` (read by `SettlementCaptureAttempt.r_multiple`/`.return_pct` already, at settlement time — Phase 4.5) | ✓ nothing new to compute; `r_multiple`/`return_pct` are already the final numbers. |
| Strategy breakevens (for Breakeven Accuracy) | **Not a stored column anywhere.** Recomputable from `EntrySnapshot` rows (`option_type`, `action`, `strike`, `benchmark_entry_price`, `quantity`) via `analytics/options/payoff.py::analyze()` — the exact same engine `benchmark_entry_capture.py` already calls at capture time. | ✓ computable on read; **zero new column needed** — see §3.2. |
| Implied move at decision time (for Range Accuracy) | `VolatilitySnapshot.implied_move_pct`, joined via `DecisionSnapshot.option_snapshot_reference` | ✓ a real, already-computed, stored value — confirmed by reading `models/volatility_snapshot.py` directly. No re-derivation from IV+time needed; the real number this decision was actually generated against is one join away. |

**One real, honest gap, not silently worked around:** `option_snapshot_reference` is nullable
(`generate_decision()` can produce a decision with no grounding volatility snapshot on record).
When it's null, Range Accuracy for that decision is `None` (excluded from the rate's sample),
exactly like every other "can't grade this one honestly" case in this design — never defaulted to
some assumed implied move.

---

## 3. Performance aggregation engine (user's §1) and prediction analytics (§2)

### 3.1 Portfolio-level metrics — precise formulas

All computed over the **operative** `SettlementCaptureAttempt` per decision (`status=CAPTURED` —
mirrors `decision_lifecycle.py::has_settlement`'s own definition exactly; a `FAILED` attempt is
never counted as a data point, matching "no partial multi-leg exit counts as a real, closed
benchmark position," Phase 4.5 sec 5).

- **Total Decisions** = `count(DecisionSnapshot)` for the portfolio, all lifecycle stages
  (`PENDING_ENTRY`/`ENTERED`/`SETTLED`) included — this is "how many times has the AI been asked
  to decide," not "how many closed trades."
- **Settled Decisions** = `count(DecisionSnapshot)` with an operative `SettlementCaptureAttempt`.
  This is the denominator every metric below actually runs over.
- **Win Rate** = `count(is_win=True) / Settled Decisions`, via the same `Rate(correct, total)` /
  `.pct` pattern as `services/track_record.py` — `None` (not `0`) when `Settled Decisions == 0`.
- **Average R** = `mean(r_multiple)` over settled decisions with a non-null `r_multiple` (it's
  only null when `initial_max_risk` was itself unavailable at entry — a real, rare, honestly
  excluded case, not a data-quality assumption).
- **Median R** = `median(r_multiple)` over the same population.
- **Expectancy** (in R) = `mean(r_multiple)` over the same population — **numerically identical**
  to Average R, restated under its own name because it's the number the industry-standard
  formula `(Win% × Avg Win R) − (Loss% × Avg Loss R)` (with `Avg Loss R` as a positive magnitude
  subtracted) always reduces to, once R-multiples already carry their own sign. Reported as its
  own named field regardless, since "Expectancy" and "Average R" answer the same question from
  two conventionally different angles and both were explicitly asked for by name.
- **Profit Factor** = `sum(realized_pnl where > 0) / abs(sum(realized_pnl where < 0))`. `None`
  when the denominator (gross loss) is exactly zero — either no losses yet, or no settled
  decisions at all — never reported as infinite or as a fabricated large number.
- **Maximum Drawdown** — the one metric here that isn't a single aggregate over an unordered set;
  it requires a real, decided ordering and basis. Proposed, explicit (not silently assumed):
  1. Order settled decisions by `SettlementCaptureAttempt.captured_at` (the real moment the exit
     was captured — the only honest "when did this outcome become real" timestamp on record).
  2. Build a cumulative-P&L running series: `equity[i] = initial_capital + sum(realized_pnl[0..i])`,
     using `BenchmarkPortfolio.initial_capital` ($2,000) as the starting point — matches this
     project's own fixed-capital benchmark framing (Phase 4 sec 2.4), not an arbitrary zero base.
  3. Maximum Drawdown = `max(running_peak[i] - equity[i])` across the series, reported both as a
     dollar figure and as a percentage of the running peak at that point (`drawdown_pct =
     drawdown / running_peak * 100`) — the dollar figure is the plain, unambiguous one; the
     percentage is what most track-record dashboards actually display, so both are computed once
     and returned rather than forcing the caller to re-derive one from the other.
  This is flagged explicitly in §7 (open questions) since "ordered by what, based on what capital"
  is a real judgment call, not a fact already fixed by the schema.

### 3.2 Prediction analytics — precise formulas

- **Directional Accuracy** = fraction of settled decisions where the *sign* of the real realized
  move — `sign((SettlementCaptureAttempt.underlying_price − EntryCaptureAttempt.underlying_price)
  / EntryCaptureAttempt.underlying_price)` — matches the sign implied by `DecisionSnapshot.
  strategy_direction`, using the exact same `_DIRECTIONAL_SIGN` mapping `services/track_record.py`
  already uses (`strong_bullish`/`bullish` → +1, `bearish`/`strong_bearish` → −1, `neutral` →
  excluded, matching *"no direction was called, nothing to grade"*, not graded as correct or
  incorrect). Both entry and exit underlying prices are real, live-captured values (Phase 4.4
  hardening's `get_underlying_quote()` / Phase 4.5's own exit capture) — never a daily close,
  never estimated.
- **Breakeven Accuracy** = fraction of settled decisions where the real exit underlying price
  cleared (debit position) or stayed within (credit position) the strategy's own breakeven(s),
  mirroring `services/decision_settlement.py::_breakeven_met`'s exact debit/credit logic, but
  recomputed from real Phase 4 data instead of V3's derived `actual_price = underlying_price *
  (1 + actual_move_pct)` approximation:
  1. Rebuild `list[OptionLeg]` from the operative `EntryCaptureAttempt`'s `EntrySnapshot` rows
     (`option_type`, `action`, `strike`, `benchmark_entry_price` as `premium`, `quantity`) — the
     exact same reconstruction `benchmark_entry_capture.py` itself already does at capture time.
  2. Call `analytics/options/payoff.py::analyze()` once to get real `breakevens` and `net_premium`
     — no second, parallel breakeven-math implementation.
  3. Compare against the **real, captured** `SettlementCaptureAttempt.underlying_price` directly
     (not a derived approximation — Phase 4 has the actual number).
  `None`/excluded when `analyze()` returns no breakevens for that leg structure (a real,
  legitimate case for some strategy shapes, e.g. a naked single-leg without a defined breakeven
  the same way a spread has one — never guessed).
- **Range Accuracy** (new to Phase 4.6 — no V3 equivalent) = fraction of settled decisions where
  `abs(actual_move_pct) <= VolatilitySnapshot.implied_move_pct` (joined via `DecisionSnapshot.
  option_snapshot_reference`) — did the real outcome stay inside the option market's own implied
  range at decision time, a market-calibration question distinct from Directional Accuracy
  (which asks about sign, not magnitude) and from Breakeven Accuracy (which asks about the
  *strategy's* breakeven, not the market's implied move). Excluded (not graded) when
  `option_snapshot_reference` is null — a real, honestly-excluded gap (§2), never assumed.

---

## 4. Probability calibration (user's §3)

Compares `DecisionSnapshot.estimated_probability` against the real settlement outcome
(`SettlementCaptureAttempt.is_win`), bucketed exactly as specified: `60-70`, `70-80`, `80-90`,
`90+`. **Deliberately narrower than `services/track_record.py`'s own 5-bucket scheme** (which
also covers `0-59`), because this phase's own instruction gives four specific bucket boundaries,
not "however many buckets the data spans" — a decision below 60% estimated probability is simply
outside this report's scope, exactly as specified, not silently folded into a catch-all bucket
that wasn't asked for. Flagged in §7 as a confirmable choice, since it's a real product decision
(sub-60% decisions still exist and are still settled — this design reports zero information about
them here) rather than a technical constraint.

Per bucket: `correct = count(is_win=True AND probability in bucket)`, `total = count(probability
in bucket)`, same `Rate`/`.pct` shape as everywhere else in this document. A decision with a null
`estimated_probability` (a real, possible case — `generate_decision()` can produce no probability
estimate) is excluded from every bucket, never defaulted into one.

**What "calibration" means here, stated precisely, since the word is used loosely elsewhere in
this codebase:** for each bucket, the *realized* win rate should track the *predicted* probability
if the AI's confidence is well-calibrated (a 70-80% bucket whose real win rate comes out near 55%
is a genuine, important, honestly-reported finding — this endpoint's entire purpose is to make
that visible, not to flatter the model).

---

## 5. Strategy/confidence/DTE/risk-profile/IV-regime breakdowns (user's §4)

**Not a separate endpoint returning every bucket at once — the same aggregation function, called
per filter, matching the original plan's own query-param design** (`PHASE4_ARCHITECTURE_REVIEW.
md`'s `?strategy=&confidence_bucket=&dte=&risk_profile=&iv_regime=` sketch). `GET /benchmark/
track-record` accepts each of these as an optional filter; the response is always one summary
computed over exactly the decisions matching the filters given (or the whole portfolio, with none
given). This keeps the aggregation logic itself a single function, always operating over one
already-filtered decision set, exercised identically whether the caller wants the whole-portfolio
number or one narrow slice — no second "compute every breakdown" code path to keep consistent
with the first.

| Filter | Query param | Match against |
|---|---|---|
| Strategy | `strategy` | `DecisionSnapshot.strategy_type` (exact string match, e.g. `iron_condor`) |
| Confidence bucket | `confidence_bucket` | `DecisionSnapshot.estimated_probability` falling in one of the same four §4 buckets, by label (`"60-70"`, `"70-80"`, `"80-90"`, `"90+"`) |
| DTE bucket | `dte_bucket` | `(selected_expiration - generated_at.date()).days`, bucketed — **boundaries not specified by the brief; proposed**: `0-7`, `8-14`, `15-30`, `31+` (short/near/medium/long, a standard, round split; flagged in §7 as confirmable, not load-bearing to the rest of the design) |
| Risk profile | `risk_profile` | `BenchmarkPortfolio.risk_profile` (`conservative`/`moderate`/`aggressive`) — a join, not a `DecisionSnapshot` column (§2) |
| IV regime | `iv_regime` | `DecisionSnapshot.volatility_regime` (`high`/`normal`/`low`) |

Each filter is independently optional and combinable (e.g. `strategy=iron_condor&risk_profile=
moderate` narrows to exactly that intersection) — a plain `AND` of whichever filters are present,
no special-casing per combination.

---

## 6. Read-only API (user's §5)

```
GET /api/v1/benchmark/track-record
    ?portfolio_id=            (optional, defaults to the single active portfolio -- see §7)
    &strategy=
    &confidence_bucket=       one of "60-70" | "70-80" | "80-90" | "90+"
    &dte_bucket=              one of "0-7" | "8-14" | "15-30" | "31+"
    &risk_profile=            one of "conservative" | "moderate" | "aggressive"
    &iv_regime=               one of "high" | "normal" | "low"
  -> BenchmarkTrackRecordResponse (total_decisions, settled_decisions, win_rate, average_r,
     median_r, expectancy, profit_factor, max_drawdown, max_drawdown_pct, directional_accuracy,
     breakeven_accuracy, range_accuracy -- each a RateResponse-shaped {correct,total,pct} except
     the plain numeric ones -- see §3)

GET /api/v1/benchmark/calibration
    ?portfolio_id=            (same default as above)
  -> BenchmarkCalibrationResponse (buckets: list of {label, lower, upper, rate})
```

**No mutation endpoint of any kind** — the same standing decision every Phase 4 router has made
since `decision_snapshots.py`'s own docstring first stated it (*"not exposing a write path is a
stronger guarantee than exposing one and trusting callers not to misuse it"*): these two
endpoints only ever read `SettlementCaptureAttempt`/`EntrySnapshot`/`DecisionSnapshot`/
`BenchmarkPortfolio`/`VolatilitySnapshot`, compute in memory, and return — they never write
anything, not even a cache row.

**The "zero settled trades" constraint, enforced at the type level, not just by convention:**
every rate in the response is `{correct, total, pct: Decimal | None}` — when `settled_decisions
== 0`, every `pct` is `None` and every plain metric (`average_r`, `profit_factor`, `max_drawdown`,
...) is also `None`. The API does **not** special-case a literal `"No settled trades available"`
string response (that would mean two different response shapes for the same endpoint depending on
data volume, which every existing FastAPI route in this codebase avoids) — instead, `settled_
decisions: 0` plus every metric `null` **is** "no settled trades available," honestly represented
in the schema itself rather than as a magic string a caller has to pattern-match against. Flagged
in §7 since the brief's own wording (*"Return: 'No settled trades available'"*) could be read as
wanting that literal string — worth confirming which is actually wanted before implementation.

---

## 7. Summary — open questions requiring confirmation before coding

1. **Max Drawdown's basis (§3.1).** Recommendation: ordered by `captured_at` (settlement time),
   based on `initial_capital + cumulative realized_pnl`, reported as both a dollar figure and a
   percentage of the running peak. Confirm, or specify a different ordering/basis (e.g. ordered by
   entry time instead of settlement time, or based on `return_pct` instead of dollar P&L).
2. **The literal "No settled trades available" string (§6).** Recommendation: represent "no data"
   via `settled_decisions: 0` and every metric `null`/empty `Rate`, not a special-cased string
   response — matches how every other rate in this codebase (including V3's own `services/
   track_record.py`) already represents "no sample." Confirm this satisfies the brief's intent, or
   specify that the literal string is required (and if so, whether it replaces the whole response
   or is one field within it).
3. **DTE bucket boundaries (§5).** Proposed `0-7`/`8-14`/`15-30`/`31+` — not specified in the
   brief. Confirm, or supply different boundaries.
4. **Sub-60% probability decisions (§4).** The specified calibration buckets (`60-70` through
   `90+`) leave decisions below 60% estimated probability entirely unreported by the calibration
   endpoint. Confirm this is intentional (matches the brief's exact wording), or specify whether a
   `<60%` bucket should be added.
5. **Multi-portfolio scoping (§6).** Today there is exactly one `BenchmarkPortfolio` row
   (`is_active=True`, Moderate, $2,000 — Phase 4.4). The proposed `portfolio_id` query param
   defaults to that single active portfolio when omitted, the same resolution `services/
   scheduler.py`'s jobs already use. Confirm this default is acceptable, or specify that
   `portfolio_id` should be required.
6. **Frontend (user's §7 — "only if existing dashboard architecture supports it").** No Phase 4
   frontend work exists yet in this branch at all — `frontend/src/App.tsx` has no `earnings-
   calendar`, `benchmark-portfolio`, or any Phase-4-scoped route (confirmed directly). "Existing
   dashboard architecture" can therefore only mean V3's own generic page/component patterns
   (`TrackRecord.tsx`'s layout, `Layout`'s route shell, chart components), not a Phase-4-specific
   dashboard that's already there to extend. **Recommendation: no frontend in Phase 4.6 either**,
   consistent with every prior Phase 4 sub-phase (4.1–4.5 all explicitly excluded frontend work) —
   this phase ships the real, honest data and the read API over it; a dedicated frontend phase
   (already sketched in `PHASE4_ARCHITECTURE_REVIEW.md` §7: a new `BenchmarkPortfolio.tsx` page,
   explicitly *not* a tab bolted onto the existing `TrackRecord.tsx`, since they grade different
   things) is better scoped as its own pass once this phase's real endpoints exist to build
   against. Confirm this reading, or specify a minimal frontend addition to build now.
7. **Module naming/placement.** Proposed: `analytics/decision/track_record_math.py` (pure
   formulas — `Rate`, bucket dataclasses, drawdown/profit-factor/expectancy functions, no DB
   access) + `services/benchmark_track_record.py` (DB querying, filter application, orchestration
   — mirrors the `settlement_math.py` / `benchmark_exit_capture.py` split Phase 4.5 already
   established) + `api/routers/benchmark_track_record.py` (new file, same `/benchmark` prefix
   `benchmark_entries.py` already uses). No new migration — every field this phase needs already
   exists on real, already-migrated tables (§2); this phase is pure read-side computation.

Do not start coding until these are resolved.
