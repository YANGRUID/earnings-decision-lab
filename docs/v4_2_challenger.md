# V4.2 Challenger — design and status

**Not production.** v4.1.0 remains the control methodology. This document records what the
2026-09-05 forensic audit proved, what the challenger changes in response, and — importantly —
what currently blocks it.

## The defects this responds to

All three were established from the production database and the source, not inferred from
outcomes.

**1. There is no absolute economic viability gate.** `classify_candidate_validity` says so in its
own docstring: it is "data honesty only" and "deliberately says nothing about whether the
candidate is economically attractive". `rank_candidates` only sorts. The decision layer takes
rank #1 whenever at least one candidate is *honestly rankable*, so `NO_ACTION` is reachable only
through missing data — never through bad economics.

Consequence, over the first 7 natural events: all 7 selected candidates had a negative modeled
median executable T+1 return, and 2 (DOCU, ZS) were selected with `no_profitable_region` — the
engine's own valuation said no modeled scenario made money, best cases −2.12% and −1.86%.

**2. Semantics dominates economics lexicographically.** `build_ranking_key` is a tuple sorted
descending with `_semantic_band` first, so a candidate in a higher semantic band can never be
outranked by better economics, however large the gap. In 5 of 7 events a materially better
ex-ante candidate existed in the same rankable set; twice it had a *positive* modeled median and
was passed over for a negative one (GWRE +4.21% → −8.22%; CPRT +0.09% → −1.77%).

**3. A qualitative volatility label is accepted as an edge.** `derive_v4_market_view` maps
`long_vol → large_move` and `short_vol → small_move`, and semantic compatibility scores strategy
fit against that label. Nothing anywhere compares expected move to the option market's *implied*
move. A "large move" view justifies a long strangle even when the market already implies a
larger move.

## What the challenger changes

| Version key | Value |
|---|---|
| `VIABILITY_GATE_VERSION` | `v4_2_viability_gate_v1` |
| `MOVE_EDGE_VERSION` | `v4_2_move_edge_v1` |

**Gate before rank.** `choose_v4_2_candidate` applies an absolute, per-candidate economic gate
first; only candidates that clear it are ranked, and if none clear it the result is `NO_ACTION`.

**Economics decides among survivors.** Semantic compatibility becomes a *gate* (a contradiction
is refused outright) rather than the dominant sort key. Among accepted candidates the best
modeled median wins, worst case breaking ties.

**A move-exposed structure must show a quantitative edge.** Long-move structures require
expected/implied > 1 + margin; short-move structures require < 1 − margin. Where no quantitative
expected move can be derived, the gate returns `INSUFFICIENT_MOVE_EVIDENCE` and refuses the
structure rather than accepting the label. It never asks the language model for a number:
Python owns implied move, the historical distribution, and the edge.

### Thresholds, and why they are not fitted

Every default is an ex-ante economic statement. None was chosen by checking whether it made a
particular losing trade disappear.

| Rule | Default | Justification |
|---|---|---|
| `median > 0` | 0 | You do not knowingly open a position your own model says loses at the median. Minimal definition of a trade worth taking; not tunable. |
| positive scenario fraction > 0 | 0 | There must exist a modeled state of the world in which it profits. Tautological. |
| no profitable region | reject | Same, stated directly. |
| worst case ≥ −35% | 0.35 | A risk limit on standardized capital, set deliberately loose so the median rule does the work. |
| mean relative spread ≤ 25% | 0.25 | Round-trip friction of a quarter of mid exceeds any modeled median in the observed universe. |
| move edge margin | 0.20 | "Materially" different from what the market prices, not a rounding difference. |

## Status: the challenger currently trades nothing, and that is the finding

Replayed over the 7 frozen events (ex-ante inputs only), the full challenger returns
`NO_ACTION` on all 7. Sensitivity, reported as ACTION counts and never against realized P&L:

| Variant | Actioned | NO_ACTION |
|---|---|---|
| default (median > 0, move-edge on) | 0 | 7 |
| economic gate only, move-edge off | 1 | 6 |
| median > +1% | 0 | 7 |
| median > −1% | 0 | 7 |
| median > −5% | 3 | 4 |
| spread cap 0.15 | 0 | 7 |
| worst-case cap 0.20 | 0 | 7 |
| no-profitable-region gate **alone** | 7 | 0 |

Two things follow, and they must not be conflated.

**The move-edge gate is currently unsatisfiable.** `historical_sample_n = 0` and
`historical_evidence_quality = "insufficient"` on *every* event — the pipeline never populated a
historical post-earnings move distribution. So no quantitative expected move exists, and every
move-exposed structure is refused for lack of evidence. That is the gate behaving correctly, but
it means **V4.2 cannot be promoted until the historical move distribution is populated.** That
is a data dependency, not a tuning question.

**The economic gate alone is very restrictive here** because the candidate universe is almost
entirely negative-expectancy ex ante: of 110 rankable candidates across 7 events, only 3 have a
positive modeled median, and the *best* worst-case in the whole universe is −1.25%. Whether that
reflects a genuinely unattractive market or a pessimistic valuation model cannot be settled at
N = 7 — see the audit report's discussion of entry-at-real-spread versus modeled exit friction.

### The minimal, non-arbitrary change

The most conservative possible gate — reject only candidates with literally no profitable modeled
scenario — combined with ranking on economics rather than semantic band, would have changed the
selected candidate in every event and improved the ex-ante modeled median in six of them:

| Event | V4.1 selected | Median | Minimal-gate pick | Median |
|---|---|---|---|---|
| AVGO | long_call | −20.37% | long_strangle | −3.42% |
| DOCU | iron_butterfly | −5.46% | long_straddle | −1.93% |
| GWRE | iron_condor | −8.22% | call_credit_spread | **+4.21%** |
| ZS | iron_butterfly | −17.03% | bull_call_spread | −7.51% |
| CPRT | long_strangle | −1.77% | call_credit_spread | **+0.09%** |
| IOT | long_strangle | −1.25% | long_strangle | −1.25% |
| LULU | long_strangle | −2.32% | long_straddle | −2.21% |

This is an **ex-ante** improvement in what the engine modeled at selection time. It is not a
claim that these positions would have made money; with N = 7 no such claim is possible.

## Not built yet

- **Parallel run.** §43/§44's design — same evidence, same DecisionView, one market observation,
  two recommendations recorded side by side — is not wired into the scheduler. `replay_all` gives
  the offline comparison; live parallel recording needs a challenger table and a read model.
- **Multi-expiry candidates.** 5 of 7 events selected an expiration that expires on the T+1
  settlement day. The audit recommends generating candidate variants across the nearest and next
  expiry and comparing them on the same T+1 objective — deliberately *not* a minimum-DTE ban.
- **Earnings-specific friction model.** The current LOW/NORMAL/HIGH = 4%/10%/18% comes from a
  general `options_snapshot` cohort (n=700). Observed short-dated earnings spreads have a much
  fatter tail (2 of 7 above the modeled HIGH; worst 40%). Rebuilding it needs a comparable
  ex-ante cohort accumulated over time — not a refit against these 7 outcomes.
