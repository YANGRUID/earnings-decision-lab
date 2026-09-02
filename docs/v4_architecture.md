# V4 Architecture

**Status: the only decision engine (V4-only reset, 2026-09-02).** V4 runs prospectively as a
forward test — no orders, no backfill. It has produced no proven performance advantage; see
[`v4_forward_testing.md`](v4_forward_testing.md) for what "proven" would require.

## One event, one evidence freeze, six results

```
earnings event (real calendar only)
  └─ research-ready gate           one check      (AIThesisVersion must exist, as_of-safe)
     └─ DecisionView               one LLM call   (DeepSeek; direction / volatility / move intent / confidence)
        └─ underlying observation  one quote      (TWS; DELAYED stays labelled DELAYED)
           └─ expected-move context               (implied move from the chain, historical median move)
              └─ candidate geometry universe      (strikes placed relative to ±EM, bounded ≤ 60)
                 └─ exact contract resolution     (conId per leg, deduplicated by (strike, right))
                    └─ option quote acquisition   one batched TWS sweep, deduplicated
                       └─ T+1 scenario valuation  per candidate: 7 moves × 3 IV levels (core) + ±1.5/±2 EM (stress)
                          └─ V4.4B ranking v1     banded lexicographic; unchanged since freeze
                             └─ SIX configuration evaluations   pure, in-memory, no I/O
                                └─ SIX V4ShadowConfigResult rows  (+ one V4ShadowDecision, shared candidates)
```

Everything above the last two lines happens **once** per event. The six configurations are
filter-and-sort passes over the same in-memory candidate list; a test refuses socket connections
during evaluation to prove it. Six independent pipelines would mean six LLM calls, six quote sweeps,
six different timestamps — and six results that were no longer comparable to each other.

## Modules

| Layer | Module | Notes |
|---|---|---|
| Research / RAG | `services/research_orchestration.py`, `rag/` | company-scoped, `as_of`-filtered |
| DecisionView | `services/v4_shadow_orchestration.py::default_view_generator` | reuses `prompts.decision_view` (`decision-view-v1`) |
| Expected move | `analytics/decision/v4_expected_move.py` | implied + historical, frozen on the decision row |
| Semantic compatibility | `analytics/decision/v4_semantic_compatibility.py` | view ↔ structure fit |
| Strike geometry | `analytics/decision/v4_strike_engine.py` | candidates placed against ±EM |
| T+1 valuation | `analytics/decision/v4_t1_pricing.py`, `v4_t1_scenario_grid.py`, `v4_t1_stress_grid.py` | core and stress kept separate |
| Ranking v1 | `analytics/decision/v4_4b_ranking.py` | `v4-4b-t1-executable-ranking-v1`, frozen |
| Six configurations | `analytics/decision/v4_configurations.py`, `services/v4_config_evaluation.py` | pure layer above the ranker |
| Timing policy | `analytics/decision_timing_policy.py`, `analytics/forward_windows.py` | 15:30 ET entry, 15:30 ET T+1 settlement (`v4-1530-entry-1530-t1-settlement-v2`); window tolerances and the 15:50 ET deadline |
| Shadow evidence | `models/v4_shadow.py`, `services/v4_shadow.py` | append-only, DB trigger enforced |
| Settlement | `services/v4_shadow_cohort.py` | re-quotes frozen conIds per configuration; never a reconstruction |
| Scheduler | `services/v4_shadow_scheduler.py` | dedicated scheduler DB pool; registered only when enabled |
| Read models | `api/routers/v4_shadow.py`, `api/routers/operations.py` | six-config, track record by configuration, V4 pipeline states and readiness |

## Evidence tables

| Table | Cardinality | Holds |
|---|---|---|
| `v4_shadow_decision` | 1 per event/window/engine | DecisionView, LLM provenance, underlying, expected move, every version stamp, latency, TWS request budget, **timing policy version** |
| `v4_shadow_candidate` | N per decision (shared) | ranking dimensions, core/stress aggregates, **per-scenario grid** |
| `v4_shadow_candidate_leg` | legs per candidate | conId, bid/ask, required side, greeks, quality, provider, timestamp |
| `v4_shadow_config_result` | **6 per decision** | configuration identity, status, rank #1, exclusions — nothing that is common |
| `v4_shadow_observation` | 1 per decision per phase | ENTRY / EXIT executable observation |
| `v4_shadow_settlement` | 1 per decision | T+1 outcome |
| `v4_shadow_run_event` | any | failures and notices, by category |

All seven are append-only: a `BEFORE UPDATE` trigger rejects edits.

## Isolation and safety

- V4 jobs are registered last, in their own `try/except`; a V4 registration failure cannot take
  the platform jobs (calendar sync, research preparation, IBKR health) down.
- V4 writes happen inside a SAVEPOINT; a V4 failure cannot unwind a caller's transaction.
- V4 scheduler work uses the dedicated scheduler DB pool; API requests use the API pool.
- The test suite rebinds both session factories to the disposable test database, so no test can
  reach production.
