# V4.4C — Shadow methodology & evidence architecture

**Status: authoritative reference for the V4 shadow forward test.**
**Shadow generation is DISABLED (`V4_SHADOW_ENABLED=false`) and must stay disabled until
activation is explicitly authorized.**

V3 remains the official forward-test engine. V4 is experimental. Nothing described here places a
brokerage order, writes to a V3 table, or affects official execution.

---

## 1. Why shadow testing is needed

V4.4B's historical replay could not answer the questions that matter, and the reason was
structural rather than fixable:

| Gap | Consequence |
|---|---|
| The V4 market view was never persisted | V4.2 semantics were **inert** across the entire replay |
| Only V3's own chosen candidate survived | No competing set existed, so **no true head-to-head** |
| Point-in-time chains were incomplete | 10 of 23 decisions **could not be replayed honestly** |

Reconstruction after the fact cannot repair any of that without fabricating data. Forward shadow
evidence can — but only if the inputs are frozen *at the moment of decision*. That is what this
phase builds.

The system must be able to answer, from immutable forward evidence: at the same legal timestamp,
from the same point-in-time state — what did V3 officially choose, what would V4 have chosen, why,
what candidate set did V4 consider, what quotes did it see, what was the ranking, and what
happened at T+1?

## 2. V3 control vs V4 shadow

| | V3 | V4 shadow |
|---|---|---|
| Role | **Official** forward test | **Experimental** observation |
| Tables | `decision_snapshot`, `entry_snapshot`, … | `v4_shadow_*` (entirely separate) |
| Brokerage order | none (read-only benchmark) | none — and no order surface exists |
| Timestamp | official 15:55 ET window | **the same window**, never earlier |
| Failure impact | official pipeline | isolated; cannot affect V3 |
| Cohort label | Benchmark Track Record | **V4 Experimental Shadow** |

Cohorts are never merged. Comparison keys on `earnings_calendar_event_id` — the authoritative
event identity, never a ticker/date match.

## 3. Point-in-time guarantees

Every shadow decision uses only data available at the legal decision timestamp. No future filing,
price, quote, or post-earnings evidence may enter. When data is missing it is **recorded as
missing** and never backfilled into a frozen candidate afterwards.

Enforced structurally, not by discipline:

- `services/v4_shadow.py`, `v4_4b_ranking.py`, and `v4_t1_stress_grid.py` import **no** settlement,
  exit, price-reaction, or outcome module — asserted by AST tests.
- `V4ShadowDecision` and `V4ShadowCandidate` have **no column** capable of holding a realized
  outcome — asserted by a schema-shape test.
- Realized outcome lives **only** on `V4ShadowSettlement`, written long after the freeze.

## 4. Candidate-set freeze

The **complete** honestly-generated candidate set is frozen, not merely rank #1 — discarding the
rest is exactly what made V4.4B's replay unable to compare anything.

Per candidate: strategy, expiration, geometry variant, rank, validity state, semantic
compatibility, core T+1 economics, tail stress, execution quality, capital usage, the serialized
`ranking_key`, a deterministic rank explanation, and data-quality warnings.

Per leg: action, right, strike, quantity, conId, **required side and its price**, bid/ask/last, IV,
Greeks, market-data quality, source provider, and a genuinely **per-leg** `retrieved_at` — never
one aggregate timestamp copied across legs, which is what made cross-leg skew unmeasurable before.

## 5. Tail-stress methodology

V4.4B named the ±1.0 expected-move envelope as a limitation. V4.4C adds ±1.5 and ±2.0 EM as
**deterministic stress points**.

**The justification is structural, not empirical.** An implied move is roughly a one-sigma
expectation and earnings distributions have fat tails, so outcomes beyond it are ordinary rather
than exotic. The magnitudes were **not** chosen because any historical trade lost money, and the
module imports no outcome data of any kind.

**Core and stress are never mixed.** V4.4A's seven-point core grid is preserved exactly, and every
statistic V4.4B ranks on is still computed from core scenarios **only**. This matters more than it
appears: folding stress points into the same unweighted pool would silently move the median and
the positive-scenario fraction — a methodology change disguised as extra data. Because the inputs
are untouched, **V4.4B's ranking version v1 stays frozen**.

Stress points carry **no probability mass**. `stress_large_move_survival` is a coverage count over
deterministic points, never a probability. `scenario_grid_version` is bumped to
`v4-t1-scenario-grid-v2-core-plus-stress` and persisted on every candidate.

The IV-crush grid (0.55 / 0.75 / 1.10) is **unchanged**, and `iv_scenario_version` is persisted.

## 6. Entry and exit observation rules

Called **observations**, never fills — no order is submitted, and the schema has no `fill`,
`order_id`, or `position` column.

- **Entry:** buy → **ASK**, sell → **BID**.
- **Exit (T+1):** closing a long sells into the **BID**; closing a short buys back at the **ASK**.
- Missing required side → `NOT_EXECUTABLE`. **No midpoint, no last-price fallback, no theoretical
  expiration value, no next-day backfill** to make the dataset prettier.
- Settlement timing follows the existing benchmark policy (first post-earnings trading day close,
  ~15:55 ET) so V3 and V4 stay comparable. V4 gets no easier exit.

## 7. Immutability

All six tables are append-only and carry the project's own `reject_snapshot_update()` BEFORE
UPDATE trigger — the same DB-level guard `decision_snapshot`, `entry_snapshot`, and the
capture-attempt tables already use. Verified live: an `UPDATE` is rejected by Postgres.

Retry and failure history is **appended** to `V4ShadowRunEvent`; a frozen decision is never
mutated. No model carries `updated_at`, asserted by test.

## 8. Versioning

Every shadow decision persists the full version set needed to reproduce its ranking:
engine, shadow schema, strategy semantics, compatibility, expected move, strike engine, geometry,
valuation, scenario grid, IV scenario, ranking, prompt version, and LLM provider/model. No silent
drift is possible, because a stored decision states its own provenance.

## 9. Scheduler architecture (designed, not activated)

```
official decision window (15:55 ET)
        |
        +--> V3 OFFICIAL PATH        (priority; unchanged)
        |
        +--> V4 SHADOW PATH          (only if V4_SHADOW_ENABLED)
                 independent attempt/result, independent failure record
```

Proposed job names: `v4_shadow_decision`, `v4_shadow_settlement` — deliberately distinct so they
never overload the official V3 job's success/failure counters.

**Latency safety:** V3 runs first and has priority; V4 shadow consumes only the remaining legal
window. Concurrency is *not* assumed free and is not used.

**Research dependency:** shadow generation may run only when research is ready; otherwise it
records `RESEARCH_NOT_READY` rather than building a view from insufficient evidence.

**Boundary:** shadow ranking belongs to the decision scheduler path, never to research-worker — a
precomputed ranking would be stale by 15:55 ET.

## 10. Failure isolation

A V4 failure must never break, delay, or fail V3.

- `generate_shadow_decision` never raises at its boundary; failures become recorded evidence.
- Shadow writes run inside a **SAVEPOINT**. This is deliberate: a plain `db.rollback()` in the
  handler would unwind the *entire* surrounding transaction, which in the scheduler is shared with
  other work — so a shadow bug could silently discard unrelated (potentially official) writes.
  A nested transaction confines the blast radius to shadow rows alone.
- Operations reports shadow health as a **separate domain**. A shadow failure yields
  `V4 SHADOW DEGRADED`, never `OFFICIAL PIPELINE CRITICAL`.

### Failure taxonomy

`RESEARCH_NOT_READY`, `VIEW_GENERATION_FAILED`, `MARKET_DATA_UNAVAILABLE`,
`CHAIN_METADATA_FAILED`, `NO_VALID_CANDIDATE`, `QUOTE_INCOMPLETE`, `VALUATION_FAILED`,
`RANKING_FAILED`, `ENTRY_OBSERVATION_FAILED`, `SETTLEMENT_OBSERVATION_FAILED`, `INTERNAL_ERROR`.

**`NO_ACTION` is deliberately not in that list.** A valid no-action decision is an *outcome*, not a
failed run — the same distinction already made for V3. Likewise `ALREADY_GENERATED`: a scheduler
retry hitting an existing frozen window is correct behaviour, not an error.

## 11. Idempotency

`UNIQUE (earnings_calendar_event_id, legal_decision_window_at, engine_version)` on
`V4ShadowDecision`, plus a cheap pre-check so a retry does not redo the whole valuation just to be
rejected, plus an `IntegrityError` catch as a race safety net. Observations are unique per
`(decision, phase)`.

## 12. Market-data provenance

Production TWS data is **delayed**. Every shadow decision and leg persists
`market_data_quality` and `source_provider = "ibkr_tws"` explicitly, never inferred later — so a
future analysis can stratify delayed vs live if entitlements change.

## 13. Request budget

Shadow generation performs **no** market-data fetching of its own: candidates arrive fully
resolved from real point-in-time quotes, which keeps the request budget owned in one place and
reuses the existing production shared TWS provider (client id 101). No separate process, no
per-candidate connection, no per-job connection. Candidate count is capped at `MAX_CANDIDATES = 60`,
and truncation is always logged as a run event, never silent.

## 14. Limitations (honest)

1. **Live end-to-end candidate generation is not yet wired.** The freeze/rank/observe machinery is
   built and tested, but the code path that assembles candidates from a live TWS chain at 15:55 ET
   is not implemented.
2. **Scheduler jobs are designed, not implemented.**
3. **No shadow UI yet.**
4. **No live dry-run performance measurement** has been taken, so the latency budget is unproven
   against real market data.
5. **No shadow settlement job**, so no realized shadow outcome exists yet.
6. Stress scenarios remain uncalibrated and are diagnostics only.

## 15. Future calibration

The schema is deliberately shaped so a future calibration dataset needs no migration per phase:
market view, candidate features, rank, quotes, scenario surface, execution quality, and (after
settlement) the realized T+1 result are all already persisted and joinable. **Nothing is trained
now**, and outcome fields remain unreachable from decision-time code.
