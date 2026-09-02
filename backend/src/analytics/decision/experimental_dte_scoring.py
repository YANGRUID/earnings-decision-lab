"""Phase 4 methodology-experiments hardening (2026-08-26), Section 36 --
EXPERIMENTAL, strategy-family-aware DTE (days-to-expiration) scoring.

The official ranking (analytics/decision/strategy_scoring.py::
_expiration_fit) applies one generic 7-21-days-after-earnings sweet spot
to every strategy category. This module is a versioned, standalone
alternative that scores DTE suitability per strategy FAMILY instead --
never wired into strategy_scoring.py, generate_strategy_candidates, or
any official ranking path. Its per-family windows below are a documented
HYPOTHESIS grounded in standard options-theory reasoning, not a
validated finding: this project does not yet have enough real, settled
forward-test decisions to calibrate these empirically (see Stage 3 of
this phase's own final report -- "data-driven ranking weights once
structural cells have sufficient samples"). Do not activate this in
official ranking until real evaluation data (Section 26's own gating
principle, applied here) justifies it.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Literal

from analytics.options.strategy_candidates import StrategyCategory

EXPERIMENTAL_DTE_SCORING_VERSION = "experimental-dte-v1"

StrategyFamily = Literal["long_vol", "short_vol", "directional_debit", "defined_risk_neutral_range"]

# A clean, non-overlapping partition of every real StrategyCategory this
# project generates (analytics/options/strategy_candidates.py) into the
# four families Section 36 names.
_FAMILY_BY_CATEGORY: dict[StrategyCategory, StrategyFamily] = {
    StrategyCategory.LONG_STRADDLE: "long_vol",
    StrategyCategory.LONG_STRANGLE: "long_vol",
    StrategyCategory.PUT_CREDIT_SPREAD: "short_vol",
    StrategyCategory.CALL_CREDIT_SPREAD: "short_vol",
    StrategyCategory.LONG_CALL: "directional_debit",
    StrategyCategory.LONG_PUT: "directional_debit",
    StrategyCategory.BULL_CALL_SPREAD: "directional_debit",
    StrategyCategory.BEAR_PUT_SPREAD: "directional_debit",
    StrategyCategory.IRON_CONDOR: "defined_risk_neutral_range",
    StrategyCategory.IRON_BUTTERFLY: "defined_risk_neutral_range",
    StrategyCategory.LONG_CALL_BUTTERFLY: "defined_risk_neutral_range",
}


@dataclass(frozen=True)
class DteSweetSpot:
    low: int
    high: int
    max_considered: int
    rationale: str


# HYPOTHESIS, not validated (see module docstring). Reasoning:
#
# long_vol (long straddle/strangle -- betting realized move exceeds
# implied): wants the FIRST viable post-earnings expiration, minimizing
# extra time premium paid for days beyond the event that don't
# contribute to the move being measured -- narrower, closer window than
# the official generic one.
#
# short_vol (credit spreads -- selling elevated pre-earnings IV,
# profiting from the post-earnings IV crush): wants an expiration close
# enough to capture the crush without so much time that theta decay
# before the event dominates the trade's own economics -- similar to
# long_vol but slightly wider to allow for position management.
#
# directional_debit (long call/put, debit spreads -- betting on
# direction): needs enough time for the directional move to develop
# without paying excessive extra premium -- closely matches the
# official generic window, since that window's own original reasoning
# ("enough time for the event to matter without excessive extra time
# premium") was implicitly written with a directional bet in mind.
#
# defined_risk_neutral_range (iron condor/butterfly -- betting price
# stays in a range): benefits from slightly more time for the
# range-bound thesis to prove out and reduced gamma risk immediately
# after the event -- wider, later window than the others.
EXPERIMENTAL_SWEET_SPOTS: dict[StrategyFamily, DteSweetSpot] = {
    "long_vol": DteSweetSpot(
        low=1,
        high=10,
        max_considered=30,
        rationale="first viable post-earnings expiration; minimize unmeasured extra time premium",
    ),
    "short_vol": DteSweetSpot(
        low=3,
        high=14,
        max_considered=45,
        rationale="close enough to capture post-earnings IV crush without excess pre-event decay",
    ),
    "directional_debit": DteSweetSpot(
        low=7,
        high=21,
        max_considered=60,
        rationale="matches the official generic window -- written with a directional bet in mind",
    ),
    "defined_risk_neutral_range": DteSweetSpot(
        low=10,
        high=25,
        max_considered=60,
        rationale="more time for a range-bound thesis to prove out; reduced post-event gamma risk",
    ),
}


def strategy_family_for(category: StrategyCategory) -> StrategyFamily:
    return _FAMILY_BY_CATEGORY[category]


def experimental_dte_fit_score(
    category: StrategyCategory,
    expiration: date,
    earnings_date: date | None,
    *,
    max_score: int = 20,
) -> int:
    """Mirrors analytics/decision/strategy_scoring.py::_expiration_fit's
    own shape (full weight inside the family's sweet spot, decaying
    toward the edges, zero for an expiration that doesn't cover the
    event) but keyed on the family-specific window above instead of one
    generic window. ``max_score`` defaults to the official WEIGHT_
    EXPIRATION_FIT so a real side-by-side comparison is on the same
    scale; never imported from strategy_scoring.py to avoid coupling
    this experimental module's behavior to a change in the official
    one's weight.
    """
    if earnings_date is None:
        return round(max_score * Decimal("0.5"))

    days_after_earnings = (expiration - earnings_date).days
    if days_after_earnings < 1:
        return 0

    spot = EXPERIMENTAL_SWEET_SPOTS[strategy_family_for(category)]
    if spot.low <= days_after_earnings <= spot.high:
        return max_score
    if days_after_earnings < spot.low:
        fraction = Decimal(days_after_earnings) / Decimal(spot.low)
        return round(max_score * max(Decimal("0.3"), fraction))
    excess = min(days_after_earnings - spot.high, spot.max_considered - spot.high)
    fraction = 1 - Decimal(excess) / Decimal(spot.max_considered - spot.high)
    return round(max_score * max(Decimal("0"), fraction))
