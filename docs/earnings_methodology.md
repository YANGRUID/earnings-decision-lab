# Earnings analytics methodology

Covers `backend/src/analytics/earnings/iv_crush.py` and the historical-move statistics in
`backend/src/analytics/earnings/historical_moves.py`. Deterministic Python throughout — no LLM
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

## Event replay — retired

The rule-based historical strategy replay (`analytics/options/replay.py`, the `strategy_replay`
table and the Cross-Company Replay screen) was removed in the V4-only reset of 2026-09-02. It
never ran against real historical options chains (none were ever wired up), and the V4 forward
test replaces reconstruction with prospective observation. The code remains on
`archive/pre-v4-only-reset`.

## Current data status — important

**No historical options-chain data source is wired up** (see [data_sources.md](data_sources.md)
— every free option evaluated either lacks historical coverage or requires a paid subscription
like ORATS/CBOE DataShop). This means:

- `iv_crush.py` is implemented and unit-tested against clearly-labeled synthetic strike/IV data
  (see `tests/test_analytics_earnings_iv_crush.py`) — never against data presented as real.
- The "how large is typical IV crush for ticker X" and "how often did the straddle underprice
  the event" questions this project set out to answer **cannot be answered with real numbers
  yet**. The functions that would answer them are built and tested; they have no real data to
  run against.

This is intentional, not an oversight: per this project's rule against fabricating data, the
forward test observes real chains prospectively instead of reconstructing history.
