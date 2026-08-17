# Earnings analytics methodology

Covers `backend/src/analytics/earnings/iv_crush.py` and
`backend/src/analytics/options/replay.py`. Deterministic Python throughout — no LLM
involvement, per this project's core rule.

## IV crush (`iv_crush.py`)

`calculate_iv_crush(pre_event_iv, post_event_iv)` computes the absolute and relative change in
ATM implied volatility across an earnings event — the standard "IV crush" measurement. Takes
plain `Decimal` inputs (not ORM objects), so it works identically whether the IVs came from a
real options-chain provider or, in tests, fixture values.

## Implied vs. realised (`iv_crush.py`)

`compare_implied_vs_realised(implied_move_pct, realised_move_pct)` answers "did the options
market over- or under-price this event?" — `error = realised − implied`; positive means the
straddle underpriced the actual move, negative means it overpriced. A ±0.5-point band around
zero is classified `"accurate"` rather than treating any nonzero error as a verdict, since
real-world implied-move pricing is never exact to the basis point.

`summarize_history(records)` aggregates a list of per-event results into average implied vs.
realised move, counts by verdict, and average IV crush — directly answering the questions this
project set out to answer ("how often did the straddle underprice the event," "how large is
typical IV crush for a ticker").

## Event replay (`replay.py`)

**Strike selection is rule-based and deterministic** — never chosen with knowledge of an
event's actual outcome. Three rules are implemented, selected through one entry point
(`select_strike`) so every replay's strike choice is auditable to the same code path:

- `nearest_atm` — nearest available strike to the underlying price at entry.
- `fixed_pct_otm` — nearest available strike to `underlying × (1 ± pct)`.
- `nearest_to_target` — nearest available strike to an arbitrary target (e.g. a fixed-delta
  target, if reliable historical delta data is ever available).

`build_replay()` reconstructs entry economics (net premium, max profit/loss, breakevens) using
the same `analytics.options.payoff` engine from Phase 3, and — only if an evaluation price is
explicitly supplied by the caller (e.g. the actual post-earnings close) — the resulting payoff.
The function never selects that evaluation price itself; a caller cannot "cherry-pick" a
flattering outcome through this API because the evaluation price is always an input, not a
search.

## Current data status — important

**No historical options-chain data source is wired up** (see [data_sources.md](data_sources.md)
— every free option evaluated either lacks historical coverage or requires a paid subscription
like ORATS/CBOE DataShop). This means:

- `iv_crush.py` and `replay.py` are implemented and unit-tested against clearly-labeled
  synthetic strike/IV data (see `tests/test_analytics_earnings_iv_crush.py` and
  `tests/test_analytics_options_replay.py`) — never against data presented as real.
- The `strategy_replay` table (added in this phase's migration) exists and is ready to store
  real results, but **contains zero rows** — there is no real historical options chain to
  reconstruct a strategy from.
- The "how large is typical IV crush for ticker X" and "how often did the straddle underprice
  the event" questions this project set out to answer **cannot be answered with real numbers
  yet**. The functions that would answer them are built and tested; they have no real data to
  run against.

This is intentional, not an oversight: per this project's rule against fabricating data, it is
better to ship a working, tested engine with an honestly-empty results table than to backfill
`strategy_replay` with invented strikes and prices. The moment a historical options-chain
provider is wired up, this phase's code runs against real data without changes.
