"""Generic options-strategy payoff engine.

Every strategy (long call, iron condor, whatever) is just a list of
``OptionLeg``s; payoff-at-expiration is additive across legs. Max
profit/loss and breakevens are derived, not hand-coded per strategy: a
payoff-at-expiration function built from long/short calls and puts is
piecewise *linear*, so every extremum occurs either at a leg's strike price,
at the S=0 floor (a stock price can't go negative), or — if the payoff is
genuinely unbounded — along the asymptotic slope as S -> infinity. Evaluating
at every strike plus S=0, and separately checking the asymptotic slope,
therefore finds every extremum and breakeven exactly, for any combination of
legs, without per-strategy formulas that would need to be independently
re-derived and re-tested for each of the nine required strategies.
"""

import enum
from dataclasses import dataclass
from decimal import Decimal

from models.enums import OptionType


class Action(enum.StrEnum):
    BUY = "buy"
    SELL = "sell"


@dataclass(frozen=True)
class OptionLeg:
    option_type: OptionType
    action: Action
    strike: Decimal
    premium: Decimal
    quantity: int = 1

    def __post_init__(self) -> None:
        if self.strike <= 0:
            raise ValueError(f"strike must be positive, got {self.strike}")
        if self.premium < 0:
            raise ValueError(f"premium cannot be negative, got {self.premium}")
        if self.quantity <= 0:
            raise ValueError(f"quantity must be positive, got {self.quantity}")

    def payoff(self, underlying_price: Decimal) -> Decimal:
        if self.option_type == OptionType.CALL:
            intrinsic = max(underlying_price - self.strike, Decimal(0))
        else:
            intrinsic = max(self.strike - underlying_price, Decimal(0))
        if self.action == Action.BUY:
            return (intrinsic - self.premium) * self.quantity
        return (self.premium - intrinsic) * self.quantity

    @property
    def signed_premium(self) -> Decimal:
        """Contribution to net premium: positive = cost (debit), negative = credit."""
        sign = 1 if self.action == Action.BUY else -1
        return sign * self.premium * self.quantity


@dataclass(frozen=True)
class StrategyAnalysis:
    legs: tuple[OptionLeg, ...]
    net_premium: Decimal  # positive = net debit paid, negative = net credit received
    max_profit: Decimal | None  # None = unbounded
    max_loss: Decimal | None  # None = unbounded (positive magnitude otherwise)
    breakevens: tuple[Decimal, ...]

    @property
    def return_on_risk(self) -> Decimal | None:
        if self.max_profit is None or self.max_loss is None or self.max_loss == 0:
            return None
        return self.max_profit / self.max_loss


def total_payoff(legs: list[OptionLeg], underlying_price: Decimal) -> Decimal:
    return sum((leg.payoff(underlying_price) for leg in legs), Decimal(0))


def _slope_as_price_to_infinity(legs: list[OptionLeg]) -> int:
    """Net d(payoff)/dS for S beyond every strike. Only calls contribute —
    put intrinsic value is flat (zero) once S exceeds the put's strike."""
    slope = 0
    for leg in legs:
        if leg.option_type == OptionType.CALL:
            slope += leg.quantity * (1 if leg.action == Action.BUY else -1)
    return slope


def analyze(legs: list[OptionLeg]) -> StrategyAnalysis:
    if not legs:
        raise ValueError("at least one leg is required")

    net_premium = sum((leg.signed_premium for leg in legs), Decimal(0))
    strikes = sorted({leg.strike for leg in legs})
    candidates = sorted({*strikes, Decimal(0)})
    points = [(s, total_payoff(legs, s)) for s in candidates]

    slope_inf = _slope_as_price_to_infinity(legs)
    payoffs = [p for _, p in points]
    max_profit = None if slope_inf > 0 else max(payoffs)
    max_loss = None if slope_inf < 0 else -min(payoffs)
    if max_loss is not None and max_loss < 0:
        max_loss = Decimal(0)  # position can't lose money anywhere sampled
    if max_profit is not None and max_profit < 0:
        max_profit = Decimal(0)

    breakevens = _find_breakevens(points, slope_inf)
    return StrategyAnalysis(
        legs=tuple(legs),
        net_premium=net_premium,
        max_profit=max_profit,
        max_loss=max_loss,
        breakevens=breakevens,
    )


def _find_breakevens(
    points: list[tuple[Decimal, Decimal]], slope_inf: int
) -> tuple[Decimal, ...]:
    breakevens: list[Decimal] = []
    for (s1, p1), (s2, p2) in zip(points, points[1:], strict=False):
        if p1 == 0:
            breakevens.append(s1)
        elif p1 * p2 < 0:
            breakevens.append(s1 + (0 - p1) * (s2 - s1) / (p2 - p1))
    s_max, p_max = points[-1]
    if p_max == 0:
        breakevens.append(s_max)
    elif slope_inf != 0:
        candidate = s_max + (0 - p_max) / slope_inf
        if candidate > s_max:
            breakevens.append(candidate)
    # Order doesn't need preserving -- the result is sorted regardless -- so
    # dedup via a plain set rather than the "seen.add() in a boolean
    # context" idiom, which is fine at runtime but reads as a bug to a
    # type checker (set.add() returns None, not a meaningful value).
    return tuple(sorted(set(breakevens)))
