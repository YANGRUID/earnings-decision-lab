# V4 Forward Testing

## What the evidence is

- **Prospective only.** A V4 record exists only if the shadow engine observed a real market at a
  real legal window. There is no backfill and there are no simulated rows; empty states in the UI
  are genuinely empty.
- **Immutable.** Every shadow table has a database trigger that rejects updates. A frozen decision,
  candidate, configuration result or settlement can be superseded by a new row, never edited.
- **Never an order.** No brokerage order capability exists anywhere in the codebase. "Entry
  observation" means an executable quote was recorded at the required side; nothing was bought.

## Two clocks

| Cohort | Decision / entry | Settlement (T+1) | Policy version |
|---|---|---|---|
| V3 official control | 15:55 ET | 15:55 ET, first post-earnings trading day | `v3-pre-earnings-1555et-v1` |
| V4 shadow | **15:30 ET** | 15:55 ET, first post-earnings trading day | `v4-pre-earnings-1530et-v1` |

V4 observes 25 minutes earlier to leave runway for six configuration evaluations before the close.
V3 was deliberately **not** moved: doing so mid-flight would split the control cohort across two
clocks. The consequence is stated rather than hidden — V3 and V4 entry prices come from different
moments of the session, so a same-event comparison is not timestamp-identical. Settlement is the
same instant for both. Every record carries its policy version.

Historical V3 rows keep the 15:55 ET they were observed at; none are relabelled.

### Due windows

Both V4 jobs derive their windows from the **same** schedule function V3 uses
(`compute_entry_exit_schedule`), with the V4 policy passed in — so V4 can never land on a different
legal decision *day* than V3, and its settlement instant is V3's exactly.

| Job | Window | Outside the window |
|---|---|---|
| `v4_shadow_decision` (15:30 ET cron) | `entry(V4) ≤ now ≤ entry(V4) + 5 min` on the legal pre-earnings trading day | event not selected |
| `v4_shadow_settlement` (15:55 ET cron) | `exit − 5 min ≤ now ≤ exit + 5 min`, exit = 15:55 ET on the first post-earnings trading day (V3's own early tolerance and late grace) | before: left pending; after: every pending configuration is closed as a terminal `SETTLEMENT_WINDOW_MISSED` failure — no later quote is ever used as exit evidence |

A 15:30 entry is therefore never settled the same afternoon. Found live on 2026-09-02 before
activation: the decision job had reused V3's 15:55-keyed predicate (0 of 34 events selected at
15:30) and the settlement job had no exit-window guard; both are fixed and pinned by
`tests/test_v4_shadow_timing_windows.py`.

## Six configuration cohorts

| Key | Capital | Risk cap | Max risk | Liquidity floor | Families |
|---|---|---|---|---|---|
| `v4_2k_conservative` | $2,000 | 15% | $300 | 0.80 two-sided | no single-leg longs |
| `v4_2k_moderate` | $2,000 | 30% | $600 | 0.40 | all generated |
| `v4_2k_aggressive` | $2,000 | 50% | $1,000 | none | all generated |
| `v4_10k_conservative` | $10,000 | 15% | $1,500 | 0.80 | no single-leg longs |
| `v4_10k_moderate` | $10,000 | 30% | $3,000 | 0.40 | all generated |
| `v4_10k_aggressive` | $10,000 | 50% | $5,000 | none | all generated |

All six evaluate the **same** frozen evidence. Each may independently produce RANKED or
NO_ACTION; no rule is relaxed to make all six trade. NO_ACTION is evidence.

### Aggressive profile — current reality (methodology question, not resolved here)

Moderate and Aggressive currently share the **same strategy-family universe**: the only family
restriction in `risk_profile.py` is Conservative's exclusion of single-leg long calls/puts, and
no uncovered-short family exists for Aggressive to additionally permit. Aggressive therefore
differs from Moderate only through a **higher risk cap (50% vs 30%)** and **no liquidity
floor (vs 0.40)**. Naked short strategies were deliberately *not* opened merely to manufacture
differentiation. Whether Aggressive should have its own family universe is an open methodology
question for a future, explicitly versioned change.

## Sample-size honesty

Below **30 settled observations** a cohort shows `INSUFFICIENT SAMPLE` and no win rate, average or
median return, or realized P&L is displayed. No portfolio drawdown or Sharpe ratio is computed
anywhere: there is no real capital ledger, and V3's static-$2,000 portfolio accounting is not
reproduced. Until a ledger exists, metrics are per-decision and standardized.

## Activation gate

Production shadow activation requires a live, in-process, market-hours dry-run that measures
real latency and TWS request budget against live quotes and confirms zero official writes.
Until that passes, `V4_SHADOW_ENABLED` stays `false` and no shadow job is registered.
