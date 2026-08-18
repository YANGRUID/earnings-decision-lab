"""Historical Move Compatibility (Phase 14) -- explicitly NOT a backtest
and NOT a forecast. This project has no historical *options* pricing
data, so it cannot and does not simulate what any strategy would
actually have been worth in the past -- that would be a real options
backtest, which this is not. What it does instead: compares a strategy's
real breakeven distance against this company's real, already-reported
past earnings-day price moves (see analytics/earnings/historical_moves.py
and models/price_reaction.py) and reports how many of those real,
observed moves would have been large enough (or small enough) to put the
strategy in profit. Every number here is either a real recorded move or a
real breakeven already computed by analytics/options/payoff.py -- nothing
simulated, estimated, or LLM-generated.
"""

from dataclasses import dataclass
from decimal import Decimal

from analytics.options.strategy_candidates import StrategyCandidate

METHOD = "historical_move_compatibility"


@dataclass(frozen=True)
class MoveCompatibility:
    method: str
    sample_size: int
    requires_move_beyond_threshold: (
        bool  # True = debit (needs a big move); False = credit (needs to stay inside)
    )
    required_move_pct: Decimal
    compatible_count: int
    compatible_pct: Decimal
    historical_moves_pct: tuple[Decimal, ...]


def assess_move_compatibility(
    candidate: StrategyCandidate, historical_move_pcts: list[Decimal]
) -> MoveCompatibility | None:
    """Returns None -- never a fabricated result -- when the candidate has
    no real breakeven on record (shouldn't happen for the required
    strategy set, but never assumed) or when there's no historical move
    data yet for this company.

    A debit position (net_premium >= 0, paid to enter -- long calls/puts,
    straddles, strangles, debit spreads) needs the stock to move *at
    least* as far as its nearest breakeven to profit, so a historical move
    is "compatible" when its magnitude is >= that distance. A credit
    position (net_premium < 0, received to enter -- credit spreads, iron
    condor) needs the stock to stay *within* its nearest breakeven to
    profit, so compatibility flips: a historical move is compatible when
    its magnitude is < that distance.
    """
    if not candidate.analysis.breakevens or not historical_move_pcts:
        return None

    underlying = candidate.underlying_price
    distances = [abs(be - underlying) / underlying for be in candidate.analysis.breakevens]
    required_move_pct = min(distances)

    requires_move_beyond = candidate.analysis.net_premium >= 0
    if requires_move_beyond:
        compatible = [m for m in historical_move_pcts if abs(m) >= required_move_pct]
    else:
        compatible = [m for m in historical_move_pcts if abs(m) < required_move_pct]

    sample_size = len(historical_move_pcts)
    return MoveCompatibility(
        method=METHOD,
        sample_size=sample_size,
        requires_move_beyond_threshold=requires_move_beyond,
        required_move_pct=required_move_pct,
        compatible_count=len(compatible),
        compatible_pct=Decimal(len(compatible)) / Decimal(sample_size),
        historical_moves_pct=tuple(historical_move_pcts),
    )
