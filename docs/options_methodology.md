# Options analytics methodology

Covers `backend/src/analytics/options/`. Every calculation here is deterministic Python —
never an LLM — per this project's core rule that arithmetic a computer can do exactly is never
delegated to a model.

## Strategy payoff engine (`payoff.py`, `strategies.py`)

Required strategy set: Long Call, Long Put, Bull Call Spread, Bear Put Spread, Put Credit
Spread, Call Credit Spread, Long Straddle, Long Strangle, Iron Condor.

**Design:** every strategy is a list of `OptionLeg`s (type, buy/sell, strike, premium,
quantity). Payoff at expiration is additive across legs — no per-strategy payoff formula is
hand-written. Max profit, max loss, and breakevens are *derived*, using one mathematical fact:
a payoff-at-expiration function built from long/short calls and puts is **piecewise linear**,
so every extremum occurs either

- at one of the legs' strike prices (a "kink" in the piecewise-linear function),
- at the underlying-price floor of `S = 0` (a stock price can't go negative), or
- along the asymptotic slope as `S → ∞`, if the position is genuinely unbounded (e.g. a naked
  long call).

`analyze()` evaluates the payoff at every strike plus `S=0`, computes the net slope beyond the
highest strike (only long/short *call* legs contribute — put intrinsic value is flat past its
strike), and from that determines whether profit/loss is bounded, and if so, its exact value —
without needing an independent closed-form formula per strategy. Breakevens are found the same
way: linear interpolation between adjacent evaluated points where the payoff sign changes, plus
the same asymptotic-slope handling for the unbounded tail.

This was verified against hand-derived values for all nine strategies (see
`tests/test_analytics_options_payoff.py`) — e.g. a $90/$95/$105/$110 iron condor with $1/$2/
$2/$1 premiums: net credit $2, max profit $2, max loss $3 (wing width $5 − credit $2),
breakevens at $93 and $107, all independently hand-calculated and matched exactly.

**Uses `Decimal` throughout**, not `float` — strikes and premiums are discrete, exact values;
floating-point rounding has no place there.

## Black-Scholes pricing and Greeks (`black_scholes.py`)

Used only when a data provider doesn't supply Greeks directly, or to sanity-check provider
values — **never to override a real market-quoted price**. Every Black-Scholes-derived value
is tagged `GreeksSource.BLACK_SCHOLES` when persisted (`models/enums.py`), so it's never
confused with a provider-quoted Greek downstream.

**Assumptions, stated plainly:**
- **European exercise.** NVDA/AMD/MU/SNDK equity options are American-style — early exercise
  is possible. This is a genuine model/market mismatch, not a rounding error: Black-Scholes
  prices are an *approximation* for American options. It is most accurate for calls on
  non-dividend-paying stock (where early exercise is never optimal) and least accurate for
  deep ITM puts. This project does not implement a binomial/American-exercise model; it uses
  Black-Scholes and documents the mismatch rather than presenting the output as exact.
- Constant volatility, constant risk-free rate, continuous trading, no transaction costs,
  lognormal underlying-price distribution.
- Dividends via a continuous yield `q` (default 0) — a simplification of discrete dividend
  payments, adequate for short-dated options on these tickers, not exact for longer-dated ones.

**Verification:** cross-checked against a standard textbook reference case (S=K=100, T=1y,
r=5%, vol=20% → call ≈ $10.4506, put ≈ $5.5735, call delta ≈ 0.6368) and against put-call
parity (`C − P = S·e^(−qT) − K·e^(−rT)`) across several parameter sets — a much stronger
correctness check than matching one reference number, since parity must hold exactly for *any*
valid inputs if the formula implementation is correct.

**Uses `float`, not `Decimal`**, unlike the payoff engine — `math.log`/`math.exp` and
`statistics.NormalDist` need floats, and Black-Scholes inputs (implied vol, time-to-expiry as
a year-fraction) aren't exact quantities in the first place, so `Decimal`'s exactness
guarantees wouldn't apply anyway. Uses the standard-library `statistics.NormalDist` for the
normal CDF/PDF rather than adding numpy/scipy as a dependency for this alone.

## Implied move (`implied_move.py`)

**One documented methodology is implemented: the near-ATM straddle approximation** —
`implied_move_pct ≈ (ATM call mid + ATM put mid) / underlying_price`. This is a standard,
widely-used approximation, but **it is not the only correct methodology**, and this project
does not claim it is. Alternatives exist (e.g. a wider strangle-based estimate, or a
variance-swap-style calculation across the full chain) and could be added later as additional
named methods without changing this one.

Every result records `method`, `expiration`, `atm_strike`, the call/put mids used, the
underlying price, and `implied_move_pct`/`implied_move_absolute` — this is exactly the shape
`VolatilitySnapshot.inputs` (added in Phase 1's schema) exists to store, so any implied-move
figure surfaced anywhere in the system is traceable back to precisely which quotes and
expiration produced it.

**Current status:** implemented and unit-tested against realistic fixture quotes. Not yet run
against real market data — no options-chain provider is wired up yet (see
[data_sources.md](data_sources.md)); this module is ready to consume real `OptionQuote` data
the moment one is.
