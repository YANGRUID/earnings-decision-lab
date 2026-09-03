# Options Decision Engine V4 — Methodology

**Status: V4 is the only decision engine.** The V3 engine (`options-decision-engine-v3`) was
retired on 2026-09-02 (V4-only reset); its code, jobs, evidence tables and UI were removed from
the active branch and are preserved on `archive/pre-v4-only-reset`. This document keeps the
findings that motivated V4, then describes the methodology V4 runs today.

See `options_methodology.md` for the shared payoff, Black–Scholes and implied-move analytics
V4 builds on.

## Why V4 exists (findings against the retired V3 engine)

The forensic audit of V3's first 7 real settled forward-test trades (0 wins) found several
confirmed, code-grounded structural problems. They are recorded here because V4's design is a
direct answer to each:

1. Volatility fit scored LONG_VOL purely on the *sign* of a candidate's net premium — any
   net-debit structure got full credit, with no distinction between a structure that wants a
   large realized move (a straddle) and one that wants almost none (a 1-2-1 butterfly). Three of
   the seven real losses (CRM, VEEV, NVDA) traced directly to this.
2. Ranking (67 of 100 points) and the entire risk-sizing / R-multiple denominator were built on
   pure expiration-payoff intrinsic value, while the benchmark exited via a real bid/ask
   liquidation one trading session later — never at expiration.
3. Strike selection was a pure ATM-index offset over whatever strikes the chain listed — no
   expected move, IV, delta, or historical distribution entered the choice.
4. The pseudo-portfolio's cash balance was seeded at $2,000 and never debited, while the
   reported drawdown ran a sequential equity curve against that static base — two incompatible
   capital models mixed into one 460.8%-of-peak-equity figure.
5. "Estimated probability" was a raw historical base rate displayed as a calibrated probability
   of profit.
6. 22 of 23 real decisions carried `strategy_direction = NEUTRAL`: an explicit prompt nudge and
   a ranking engine where 74 of 100 points never looked at the stated view.

V4 answers these with an explicit T+1 liquidation objective, strategy semantics that separate
structure from view, expected-move strike geometry, executable bid/ask valuation, standardized
per-decision capital, and six independent configurations that may each say NO_ACTION.

## V4 benchmark objective: T+1 post-earnings liquidation

`analytics/decision/v4_methodology.py::T1_POST_EARNINGS_LIQUIDATION_V1` (a `BenchmarkObjective`)
is the explicit, documented definition every future V4 stage must be designed against:

| Field | Value |
|---|---|
| Exit policy | `ExitPolicy.FIRST_POST_EARNINGS_TRADING_DAY_CLOSE` (already real, `models/enums.py`) |
| Entry time | 15:30 ET, the last trading day before the announcement (policy `v4-1530-entry-1530-t1-settlement-v2`) |
| Exit time | 15:30 ET, the first post-earnings trading day; due exits are settled before any new decision observation |
| Open pricing | ASK (long legs) / BID (short legs) |
| Close pricing | BID (long legs) / ASK (short legs) |
| Holding period | ~1 trading session — **never** the option's own expiration |
| Market data policy | `ALLOW_DELAYED_WITH_LABEL` — delayed data is used and labelled delayed |

V4.4A values every candidate against this objective (T+1 scenario grids) and V4.4B ranks on it.

## Strategy semantics: what each structure actually pays off on

`analytics/decision/v4_strategy_semantics.py` classifies all 11 real `StrategyCategory` values
on four dimensions — directional intent, move-magnitude intent, volatility intent, and payoff
shape — derived from real payoff geometry (`analytics/options/payoff.py`), **never** from
debit/credit sign.

| Category | Directional | Move | Volatility | Payoff shape |
|---|---|---|---|---|
| `long_call` | bullish | large | long realized-move | single-sided convex |
| `long_put` | bearish | large | long realized-move | single-sided convex |
| `bull_call_spread` | bullish | moderate | mixed/path-dependent | vertical bounded directional |
| `bear_put_spread` | bearish | moderate | mixed/path-dependent | vertical bounded directional |
| `put_credit_spread` | bullish | small/pinning | short realized-move | vertical bounded directional |
| `call_credit_spread` | bearish | small/pinning | short realized-move | vertical bounded directional |
| `long_straddle` | agnostic | large | long realized-move | two-sided convex |
| `long_strangle` | agnostic | large | long realized-move | two-sided convex |
| `iron_condor` | neutral/range | range-bound | short realized-move | range credit |
| **`long_call_butterfly`** | neutral/range | **small/pinning** | **short realized-move** | tent/pinning |
| `iron_butterfly` | neutral/range | small/pinning | short realized-move | tent/pinning |

The butterfly row is the whole point: it is a **net-debit** structure that a debit/credit sign
heuristic would score as long-volatility, but its real payoff geometry is a narrow tent that pays
off near its center strike and loses on either side — the same economic bet as a short-vol
credit structure. Confirmed against real data: every one of the retired engine's 5 real butterfly
trades lost precisely because the underlying moved *more* than this tent could survive.

The V4.2 semantic-compatibility layer consumes this registry to keep a view and a structure
honest with each other.

## Capital semantics: standardized per-decision, not shared portfolio

`analytics/decision/v4_capital.py`:

- `PER_DECISION_CAPITAL = $2,000` — standardized per-decision capital, given an honest name:
  **not** a shared portfolio balance. Two concurrently-open
  decisions each independently use the full $2,000, by design, so strategy quality can be
  compared consistently regardless of how many other decisions happen to be open.
- `portfolio_simulation_available()` returns `False`, always — no code path in this backend
  reserves capital, debits/credits a shared balance, or reconstructs a true multi-position
  equity curve. Any future caller that wants a real portfolio drawdown must check this first.
- `StandardizedCohortSummary` aggregates many independent decisions (win/loss counts, mean/
  median return-on-standardized-capital) and **deliberately never computes a drawdown or equity
  curve** — exactly the computation whose absence produced the retired engine's misleading
  460.8% figure. The six-configuration track record reports counts and standardized returns
  only, and shows `INSUFFICIENT SAMPLE` below 30 settled observations.

## Probability-calibration terminology (read-side only)

The Track Record UI's "Probability Calibration" / "Predicted probability" section is renamed to
**"Historical Compatibility vs. Realized Outcome"**, with explicit copy stating this is not a
calibrated probability of profit. No stored `estimated_probability` value, no historical
snapshot, and no backend calibration math changed — this is a read-side label fix only
(`frontend/src/pages/BenchmarkTrackRecord.tsx`).

## Data-staleness: the DY source-coherence fix

The forensic audit's real example: DY's `DecisionSnapshot.underlying_price` ($380.95, from a
`VolatilitySnapshot` collected 2h40m earlier the same session) vs. the real, live
`EntryCaptureAttempt.underlying_price` ($348.25, captured moments later) — an 8.6% gap.

**Root cause, confirmed:** `resolve_best_actionable_option_market`'s CASE 1 ("market open,
current snapshot already good and priceable → use it directly, no retry") checks only
*priceability*, never *how long ago* the snapshot was actually collected — a same-session-day
snapshot from hours earlier passes identically to one from seconds ago.
`services/benchmark_entry_capture.py` always re-fetches live moments later regardless, so
decision generation's own strike/target selection can silently anchor to a materially stale
intraday price.

**Fix applied** (`services/options_reconstruction.py`, `services/decision_engine.py`): a new
`force_live_refresh` parameter, defaulting to `False` (every existing caller — Strategy Lab,
Upcoming Earnings — is unaffected). Real, official decision generation (the only caller that
actually commits a trade) passes `force_live_refresh=True`, skipping straight to the
already-existing, already-tested live-fetch-then-previous-session-fallback path instead of
silently accepting a stale "current" snapshot. Deliberately **not** a new numeric minutes-old
threshold — no such established rule exists anywhere in this codebase
(`analytics/data_state.py` and `compute_actionability` are both day/session-boundary-only) — the
official pipeline simply always prefers a fresh live read when the market is open. Regression
tests based directly on the DY failure mode live in
`tests/test_services_options_reconstruction.py`.

Separately, `analytics/decision/underlying_drift.py::compute_underlying_drift` is a read-only
diagnostic — it computes and exposes `drift_pct`/`drift_dollars` for any (decision, entry) pair
using fields that already exist (`DecisionSnapshot.underlying_price` +
`VolatilitySnapshot.snapshot_timestamp`, `EntryCaptureAttempt.underlying_price` +
`.underlying_timestamp`), with **no schema change and no enforcement threshold** — per this
task's own instruction, no established intraday market-data-age rule exists in this codebase to
anchor a rejection threshold to; that is a future methodology decision, informed by real drift
data this module makes visible.

## The V4 feature contract

`analytics/decision/v4_feature_contract.py::V4CandidateContext` is the typed input shape a
future V4.2/V4.4 candidate evaluator will eventually receive — AI view, market, event, strategy,
execution quality, and historical sub-groups, plus a **first-class** `HoldingPeriodFeatures`
group (`entry_timestamp`, `expected_exit_timestamp`, `dte_at_entry`, `dte_at_exit`,
`holding_period_seconds`). Nothing constructs one of these yet; it exists so every later V4
scoring API is designed with the real ~1-day forced exit in view from the start, rather than
bolted on after the fact the way V3's own DTE/theta reasoning never once accounted for it.

## Shadow candidates: interface only

`analytics/decision/v4_shadow.py::ShadowCandidateEvaluation` (plus
`ShadowEntryObservation`/`ShadowExitObservation`) defines the data contract a future V4.5 shadow-
candidate pipeline would populate — freezing the top-K candidates at decision time, not just the
selected one, so a future audit can finally answer "would candidate #2 have done better." No
database table, no extra IBKR quote, no scheduler load exists yet.

## What is explicitly NOT built in V4.1

| Stage | Status |
|---|---|
| V4.2 — View ↔ strategy semantic compatibility (replaces `_volatility_fit`) | `not_implemented` |
| V4.3 — Expected-move-aware strike selection | `not_implemented` |
| V4.4 — T+1-objective-aware candidate scoring/ranking | `not_implemented` |
| V4.5 — Shadow candidate data collection | `not_implemented` |
| Data-driven ranking / ML | `not_implemented`, and not until enough real official + shadow settled observations exist |

`analytics/decision/v4_methodology.py::V4_METHODOLOGY` is the single, centralized, auditable
record of exactly how much of V4 exists at any point:

```
engine_version              = options-decision-engine-v4
benchmark_objective         = t1_liquidation_v1
capital_semantics           = standardized_per_decision_v1
strategy_semantics_version  = v1
strike_engine_version       = not_implemented
ranking_version             = not_implemented
expiration_version          = not_implemented
```

No ML is trained anywhere in V4.1. No V4 recommendation, DecisionSnapshot, EntryCaptureAttempt,
or EntrySnapshot is ever produced by this task's code. Do not implement V4.2 opportunistically —
each stage above is authorized separately.
