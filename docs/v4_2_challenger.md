# V4.2 Challenger — Phase 1

**Not production.** v4.1.0 remains the control methodology and the official recommendation path.
Nothing in this document is registered, scheduled, or reachable from a running service.

## Version strings

| Component | Control (V4.1) | Challenger (V4.2) |
|---|---|---|
| Ranking | `v4-4b-t1-executable-ranking-v1` | economics-first, gate-before-rank |
| Viability gate | *(none exists)* | `v4_2_viability_gate_v1` |
| Move edge | *(none exists)* | `v4_2_move_edge_v1` |
| Move distribution | *(never populated)* | `v4_2_move_distribution_v1` |
| Expiry selection | `select_expiration_after` (nearest index) | `v4_2_expiry_ladder_v1` (foundation) |
| Friction | `t1_pricing_v1` (4/10/18%) | `earnings_friction_v2` (advisory only) |
| Timing policy | `v4-1530-entry-1530-t1-settlement-v2` | unchanged |

Unchanged and deliberately untouched: DecisionView `v4-decision-view-v1`, strategy semantics
`v4-strategy-semantics-v2`, compatibility `view_strategy_compatibility_v1`, expected move
`expected_move_v1`, strike engine `expected_move_v1`, geometry `geometry_candidate_v1`,
valuation `t1_pricing_v1`, scenario grid `v4-t1-scenario-grid-v2-core-plus-stress`.

## The three defects this responds to

All established from the production database and source, none inferred from outcomes.

1. **No absolute economic gate.** `classify_candidate_validity` says so itself — "data honesty
   only … deliberately says nothing about whether the candidate is economically attractive".
   `NO_ACTION` is reachable only through missing data. All 7 selected candidates had a negative
   modeled median before entry; 2 were selected with `no_profitable_region`.
2. **Semantics dominates economics.** `build_ranking_key` is lexicographic with the semantic band
   first, so a lower band cannot be recovered by any economic advantage. GWRE: a +4.21%-median
   candidate sat at rank 8 behind a −8.22% one.
3. **A qualitative label is accepted as an edge.** `long_vol → large_move`, with nothing comparing
   expected move to the implied move the market already prices.

## What Phase 1 built

### Point-in-time historical move distribution

The challenger's original blocker — `historical_sample_n = 0` on every event — was **not** a
missing pipeline. 1,201 `PriceReaction` rows across 50 companies were already present, including
10–48 usable observations for every V4 ticker. `assemble_shadow_candidates` is simply called
without `historical_next_day_move_pcts`, so `derive_expected_move_context` receives `None`.

That gap is **reported, not fixed** — the strike engine consults `historical_median_abs_move_pct`,
so wiring it would change V4.1's strike geometry.

What was genuinely missing is point-in-time access. `historical_moves_before` filters *strictly
before* the decision date, so an event can neither see itself nor anything reporting after it, and
the same boundary keeps returning the same sample as new events arrive.

The observation is unchanged (`PriceReaction.next_day_move_pct`) and the sample-size tiers are the
project's own `MIN_N_FOR_MEDIAN` / `QUARTILES` / `DECILES`, imported rather than restated.

**Two timing caveats travel on every distribution:**

- close-to-close differs from the live 15:30→15:30 objective by half an hour at each end;
- `announcement_time` is `UNKNOWN` for essentially the whole historical corpus, so anchoring is
  correct for AMC and shifted a session for BMO. The forward calendar that does record timing only
  reaches back to 2026-08-25 and overlaps the historical corpus in **one** row, so it cannot be
  recovered. This adds noise to the magnitude distribution; it is not a directional bias.

**Related shared defect found, not fixed:** `price_reaction_moves()` takes only `earnings_date` and
never sees `announcement_time`. For a BMO event it uses the *post-release* close as the "before"
price. Independent of V4.2 and reported separately.

### Quantitative move edge

Computed in Python from the frozen implied move and the point-in-time distribution. The model is
never asked for a number.

Applicability derives from the project's own **payoff-shape** taxonomy, not a strategy-name list:

| Payoff shape | Test | Why |
|---|---|---|
| `two_sided_convex` | long-move | profits from magnitude either way |
| `range_credit`, `tent_pinning` | short-move | profit requires the move staying small |
| `single_sided_convex` | not applicable | needs a *directional* move past its own breakeven; the median gate already prices that |
| `vertical_bounded_directional` | not applicable | bounded/threshold-shaped, including credit verticals |

The statistic is the **median** historical magnitude over implied, not an exceedance proportion:
at n = 10–48 a proportion carries a standard error of ~7–15 points while the median is stable.
Exceedance is computed and reported as a supporting diagnostic.

Results are explicit: `PASS` / `FAIL` / `INSUFFICIENT_EVIDENCE` / `NOT_APPLICABLE`, each carrying
inputs, ratio, threshold, sample size, quality tier and an explanation.

### Absolute viability gate and economics-first ranking

Order: data honesty → semantic plausibility (gate) → economic viability → move edge → liquidity →
**rank survivors** → candidate or `NO_ACTION`. Among survivors the best modeled median wins, worst
case breaking ties. Semantics gates; it no longer dominates.

`NO_ACTION` reasons are explicit: `NO_PROFITABLE_REGION`, `NO_POSITIVE_SCENARIOS`,
`NEGATIVE_MEDIAN_EXECUTABLE_RETURN`, `WORST_CASE_UNACCEPTABLE`, `ROUND_TRIP_SPREAD_UNACCEPTABLE`,
`SEMANTIC_COMPATIBILITY_UNACCEPTABLE`, `INSUFFICIENT_MOVE_EVIDENCE`, `NO_MOVE_EDGE_VS_IMPLIED`,
`CAPITAL_INCOMPATIBLE`, `RISK_CAP_EXCEEDED`, `MISSING_ECONOMICS`.

### Per-configuration outcomes

The six configurations share one evidence package and one market-data acquisition. The economic
gate is identical across them — a bad trade is bad at every size — and only capital and defined-risk
fit differ. It is a correct outcome for $2K Conservative to return `NO_ACTION` while $10K Moderate
actions the same evidence.

### Bounded expiry ladder (foundation only)

V4.1 picks the nearest listed expiry strictly after the earnings date — an index, not a comparison.
Over 7 events that selected an expiry expiring **on the T+1 settlement day** five times, which is
where the empty-book incident came from. (Note: CPRT and GWRE avoided it only because those names
carry no weeklies, not by design.)

The ladder returns the nearest 3 eligible expiries with `entry_dte`, `dte_at_settlement`, and an
explicit settlement-risk class. **It does not ban short-dated expiries** — the audit's instruction
was to compare, not legislate a minimum DTE. It is **not wired into candidate generation**: doing
that half-way would ship an official behaviour change under a challenger flag.

### Earnings friction cohort (advisory only)

Production friction (4/10/18%) is untouched. The cohort accumulates from evidence V4 already
freezes — every candidate leg persists its real entry bid and ask — so no new collection is needed.

Current state: **210 observations, 7 events, `ADVISORY_INSUFFICIENT_SAMPLE`.** It refuses to
propose levels until it holds ≥700 observations across ≥30 events, matching the evidence base of
the model it would replace.

Advisory comparison — and the nuance matters: p25 5.22%, p50 **9.52%**, p75 **16.11%**, p90 30.77%,
max 66.67%, against the incumbent's 4/10/18%. The **central** quantiles are well calibrated even
for earnings options. What the three-level model cannot express is the **tail**.

**Related gap found, not fixed:** `V4ShadowCandidateLeg` is constructed without `volume` or
`open_interest` (0 of 211 persisted legs carry either) although the provider requests the generic
ticks that supply them. The cohort accepts both and will gain those dimensions once closed.

## Replay over the seven frozen events

Ex-ante inputs only; realized outcomes joined strictly afterwards.

| Event | V4.1 selected | Modeled median | Edge ratio | Sample n | V4.2 |
|---|---|---|---|---|---|
| AVGO | long_call | −20.37% | 0.68 | 25 | NO_ACTION |
| DOCU | iron_butterfly | −5.46% | 0.68 | 16 | NO_ACTION |
| GWRE | iron_condor | −8.22% | 0.37 | 24 | **call_credit_spread** |
| ZS | iron_butterfly | −17.03% | 0.68 | 14 | NO_ACTION |
| CPRT | long_strangle | −1.77% | 0.21 | 48 | NO_ACTION |
| IOT | long_strangle | −1.25% | 1.05 | 10 | NO_ACTION |
| LULU | long_strangle | −2.32% | 0.69 | 40 | NO_ACTION |

V4.1 actioned 7/7. V4.2 actions 1/7. Every ticker's historical median magnitude sits below its
implied move (0.21–1.05) — the variance risk premium — so long-move structures fail an
evidence-based test rather than a missing-data one.

### Sensitivity — ACTION counts only

| Variant | Actioned |
|---|---|
| default | 1 |
| move-edge off (isolates the economic gate) | 1 |
| move-edge margin 0.10 / 0.30 | 1 / 1 |
| median > +1% / −1% | 1 / 1 |
| median > −5% | 4 |
| semantic floor 0.50 | 0 |
| spread cap 0.15 | 1 |
| worst-case cap 0.20 | 1 |
| no-profitable-region rule **alone** | 7 |

The result is strikingly insensitive to every threshold except the median bar itself. That is the
anti-overfitting evidence: the outcome is driven by the candidate universe being negative-median,
not by a tuned constant.

### Realized outcomes — descriptive only, joined after the freeze

V4.1's realized total across all 41 configurations was **−$18,250**. The six events V4.2 would not
have entered carry **−$14,535** between them.

**That number must not be read as vindication.** It includes LULU at **+$11,444** — the single
profitable event, which V4.2 also declines. A methodology that avoids the losers by declining
nearly everything has not been shown to be better; it has been shown to be more conservative.
Seven events cannot distinguish those.

## Promotion gates — what must be true before parallel production

1. **Chain metadata must be frozen on the decision.** Only one expiration is persisted per
   decision today, so a point-in-time multi-expiry replay is `CANNOT_REPLAY_HONESTLY`.
2. **Multi-expiry candidate generation** must be built and evaluated on the same T+1 objective.
3. **A challenger evidence table and read model** — Phase 1 replays offline; there is no persisted
   V4.1-vs-V4.2 comparison record.
4. **Volume/open-interest persistence** for exit-liquidity diagnostics.
5. **A decision on the `announcement_time` and `price_reaction_moves` BMO defects**, which affect
   the historical corpus this gate depends on.
6. **More events.** The gate's behaviour at N = 7 is a description, not a validation.

Not built in Phase 1, and not half-wired: parallel scheduler activation, challenger persistence,
multi-expiry candidate generation, frontend comparison surface.
