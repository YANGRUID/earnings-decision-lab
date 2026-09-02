# V4.4B — T+1 executable candidate ranking methodology

**Status: authoritative technical reference for V4.4B.**
**Ranking version: `v4-4b-t1-executable-ranking-v1` (frozen).**

V4.4B is the ranking layer of the V4 forward test. It creates prospective V4 evidence only and
never places orders. (The V3 engine it was originally benchmarked against was retired on
2026-09-02; see `v4_methodology.md`.)

---

## 1. Objective

V3 selected strategies largely on **expiration-payoff** properties — max profit, max loss,
payoff-shape breakevens — but was benchmarked on **next-day liquidation**. That is an objective
mismatch, and it is the specific defect V4.4B exists to correct.

The benchmark's real objective is:

> pre-earnings entry → first post-earnings trading day exit → executable bid/ask economics

V4.4B ranks against exactly that horizon, using V4.4A's T+1 scenario valuation surface as its
primary economic input. Expiration-payoff quantities remain available as descriptive risk fields
and are **not** ranking inputs.

## 2. Inputs

| Input | Source | Role |
|---|---|---|
| Strategy semantics | V4.1 `v4_strategy_semantics.py` | What a structure is *for* |
| Semantic compatibility | V4.2 `v4_compatibility.py` | View↔strategy fit, reused verbatim |
| Strikes / geometry | V4.3 / V4.3.1 | Real listed strikes, bounded candidate geometries |
| T+1 scenario surface | V4.4A `v4_t1_pricing.py` | Primary economics (21 scenarios) |
| Entry quotes | Point-in-time option quotes | Executable entry cost |
| Capital | `v4_capital.py` | Standardized per-decision capital |

**The ranking unit is a fully-specified, executable candidate** — strategy family, expiration,
real legs, real strikes, real sides, real per-leg entry quotes, its own T+1 surface, and its own
capital usage. No abstract strategy family can outrank another without its actual geometry.

## 3. Stage 1 — validity (data honesty only)

Answers *"can this be valued honestly?"* — never *"is it good?"*.

| Status | Meaning |
|---|---|
| `RANKABLE` | Fully valued on executable entry and T+1 exit economics |
| `UNCONSTRUCTABLE` | No legs |
| `QUOTE_INCOMPLETE` | A required executable side is missing → **not executable now** |
| `MISSING_IV` | No entry IV, so the scenario surface cannot be repriced |
| `INSUFFICIENT_EXPECTED_MOVE_EVIDENCE` | Underlying scenario grid unbuildable |
| `CANNOT_VALUE_HONESTLY` | Grid built, but no scenario could be valued |
| `CAPITAL_INCOMPATIBLE` | Entry cash exceeds standardized per-decision capital |

Non-rankable candidates are **returned, explained, and never scored zero**. A missing-data
candidate and a bad economic candidate are different things, and V3's habit of collapsing both to
`score=0` destroyed that distinction.

**Semantic contradiction is deliberately not a validity state.** It is an economic judgement, not
a data problem, so it is handled in stage 2 as a floor.

## 4. Stage 2 — banded lexicographic order

### Why not a weighted sum

V3 used a 100-point weighted sum of many weak components, in which a fabricated weight silently
traded a severe failure mode against a trivial convenience. Four architectures were considered:

| Architecture | Assessment |
|---|---|
| Weighted multi-objective | Rejected — reintroduces unjustifiable weights |
| Pareto dominance + tie-break | Viable, but leaves many candidates incomparable |
| Utility with explicit risk aversion | Good, but needs a utility curve nobody can justify yet |
| **Banded lexicographic** | **Adopted** |

Banded lexicographic was adopted because it is auditable (the deciding dimension is always
nameable), it makes the risk-aversion assumption explicit rather than burying it in coefficients,
and banding removes the brittleness that makes naive lexicographic ranking useless.

### The hierarchy

1. **Semantic compatibility** (V4.2)
2. **Downside** — worst modeled executable T+1 return on standardized capital
3. **T+1 economics** — median executable return
4. **Robustness** — positive-scenario coverage
5. **Execution quality** — real per-leg relative bid/ask spread
6. **Capital efficiency** — standardized capital utilisation
7. **Deterministic tie-break** — stable identifiers, never random

**The explicit trade-off:** a lower dimension can *never* compensate a higher one. That is a real,
opinionated risk-aversion choice. It is stated here rather than hidden in weights, and it is the
main thing to revisit if the ordering ever looks wrong.

### Banding

| Dimension | Band width |
|---|---|
| Semantic compatibility | V4.2's own tiers (0.25 steps) |
| Returns (downside, median) | 5 percentage points |
| Positive-scenario coverage | 10 percentage points |
| Relative spread | 5 percentage points |
| Capital utilisation | 10 percentage points |

All band widths are **HEURISTIC_UNCALIBRATED** — round, coarse, and chosen only to stop trivial
numeric noise from dominating. **None was chosen by inspecting realized outcomes.** A `None`
measurement sorts to the worst band rather than being silently treated as zero.

## 5. Semantic compatibility is a floor, not a filter

A contradictory candidate still ranks, still shows its full economics, and is still explained —
but because semantics is dimension 1, it can **never outrank a non-contradictory candidate**, no
matter how attractive its economics look.

This is precisely the V3 failure mode that put `LONG_VOL` views into `long_call_butterfly`
structures because the structure happened to be a net debit. Nothing is hidden or deleted, per the
requirement not to hard-delete without a deterministic exclusion rule in V4.2.

## 6. Why this is not an expected return, and not a probability

The 21-scenario grid (7 underlying moves × 3 IV-crush levels) has **no calibrated probability
mass**. 21 deterministic scenarios are not 21 equally-likely futures.

Therefore V4.4B **never** emits an "expected return", "probability of profit", "win rate", or
"confidence", and there is deliberately **no single composite scalar score at all** — the
hierarchy does not need one, and inventing one would invite exactly the false-precision reading
this phase exists to remove. V4.4A's `scenario_average_return` is carried as a **diagnostic** and
is not a ranking dimension.

These remain separate concepts and are never collapsed: **view** confidence, **data** confidence,
**ranking** robustness, and **probability** calibration.

## 7. Execution quality

V3's liquidity component was effectively binary — any two-sided quote anywhere earned full marks.
V4.4B measures the **actual legs the candidate would trade**: per-leg relative spread (mean and
worst), how many legs had a two-sided quote, required-side completeness, market-data quality
(including an explicit `mixed:` label when legs disagree), and cross-leg timestamp skew.

## 8. Robustness and pinning

Derived entirely from the candidate's own scenario surface — **no strategy-family-specific rule
exists anywhere**. A butterfly is disadvantaged only when its own modeled outcomes collapse
outside a narrow region.

Two failure modes are reported **separately**, because conflating them is misleading:

- `profit_concentrated_in_single_region` — profitable only if the underlying pins.
- `no_profitable_region` — profitable **nowhere** in the modeled grid.

That distinction was added after real V3 replay, where every candidate was unprofitable in every
region and a single combined flag would have stayed reassuringly silent.

## 9. Entry and exit conventions

- **Entry:** LONG uses **ASK**, SHORT uses **BID**. Never a midpoint, never a last-price fallback.
  A missing required side makes the candidate **not executable now**.
- **Exit (T+1):** closing a long uses **BID**-side economics, closing a short uses **ASK**-side —
  unchanged from V4.4A.
- Ranking prefers **executable** estimates over theoretical model value wherever both exist.

## 10. Capital

Standardized per-decision capital (`PER_DECISION_CAPITAL`). Reported as required cash, capital
utilisation, and return on standardized capital. V3's static $2,000 pseudo-portfolio accounting is
**not** reproduced, and no portfolio-level cash ledger, optimizer, cross-company allocation,
Sharpe, or Kelly sizing exists in this phase.

## 11. Market-data provenance

Production TWS data is currently **DELAYED**. Quality and provenance ride along on every candidate
and appear in the explanation and in `data_quality_warnings`. Delayed data is **neither silently
rewarded nor silently penalized**, because no documented policy for doing either exists yet.

## 12. Explainability

Every ranked candidate exposes its rank, status and reason, each ranking dimension, the exact
`ranking_key` tuple it was ordered by, execution and robustness diagnostics, capital usage, data
quality warnings, and a human rationale.

`explain_pairwise(a, b)` answers *"why did #1 beat #2"* directly by naming the **single highest
dimension on which they differ** — which is what a lexicographic order makes answerable and a
weighted sum does not.

## 13. Anti-fitting discipline

- No weight, threshold, or band was chosen by looking at realized outcomes.
- No ML, no training, no optimization.
- The ranker imports no settlement, exit, or price-reaction data — asserted structurally in
  `tests/test_v4_4b_ranking_isolation.py`.
- Historical replay runs in two separated passes: rankings are **frozen to disk before** any
  outcome is joined. Post-hoc findings are reported for a **future** phase, never used to retune
  this one.

## 14. Limitations

1. **No probability calibration.** The scenario grid is a stress/coverage device, not a
   distribution.
2. **Envelope stops at ±1.0 expected move.** Real earnings moves exceed one implied move
   regularly. Widening it to ±1.5/±2.0 as *stress* scenarios is structurally justifiable and is
   recommended for V4.4C — deliberately **not** done here, because changing the grid inside a
   ranking phase would have altered V4.4A's own unweighted statistics mid-flight.
3. **IV-crush grid is heuristic** (0.55 / 0.75 / 1.10) and inherited unchanged from V4.4A.
4. **Lexicographic ordering cannot express compensation.** A candidate marginally better on
   downside always beats one far better on median. That is intentional, and it is the first thing
   to revisit if orderings look wrong.
5. **Historical replay coverage is thin** — see the V4.4B report.
6. **Single-event only.** No portfolio claims of any kind.

## 15. Versioning

`ranking_version = "v4-4b-t1-executable-ranking-v1"`, recorded in the central
`V4_METHODOLOGY` record and asserted to match the ranker in
`tests/test_v4_4b_ranking_isolation.py`. Behaviour changes **require a new version string** —
silently reusing this one would invalidate every replay already keyed to it. Upstream versions
(V4.1–V4.4A) are unchanged; `expiration_version` remains an explicit placeholder because V4.4B did
not reintroduce V3's expiration score.

## 16. Forward validation (built)

V4 produces prospective decisions at 15:30 ET on the legal pre-earnings trading day into
immutable `v4_shadow_*` tables, observes executable entries for six configurations, and settles
at 15:30 ET on the first post-earnings trading day — with no brokerage order. See
`v4_forward_testing.md`.
