"""Options Decision Engine V3 Part E: probability/reliability metrics --
built strictly on top of existing real data (analytics/options/
move_compatibility.py's Historical Move Compatibility), never a second,
independently-invented estimate. This module answers "how confident
should you be in that percentage," not "what is the percentage" -- the
percentage itself is, and remains, MoveCompatibility.compatible_pct.

Deliberately NOT implemented here, and why: a single "market-implied
probability of profit" derived from option delta. Delta approximates the
risk-neutral probability of a SINGLE contract finishing in-the-money --
it has no well-defined translation to a multi-leg spread's probability of
NET profit without a full distributional (e.g. Black-Scholes) model this
project doesn't build for that purpose, and bolting one on would produce
exactly the kind of "fake precision" this project's own conventions
prohibit. See the Part E report for the full reasoning.
"""

from dataclasses import dataclass
from decimal import Decimal

from analytics.options.move_compatibility import MoveCompatibility

# Below this sample size, a compatible_pct is shown with an explicit "low
# sample confidence" flag rather than presented with the same visual
# weight as a well-supported estimate -- a bare percentage with no
# indication of how few real events it's built from is misleading on its
# own, regardless of how wide its confidence interval is.
LOW_SAMPLE_THRESHOLD = 20

# 95% two-sided Wilson z-score.
_Z_95 = Decimal("1.959963985")


def wilson_confidence_interval(
    successes: int, n: int, z: Decimal = _Z_95
) -> tuple[Decimal, Decimal] | None:
    """The Wilson score interval for a binomial proportion -- chosen over
    the naive normal-approximation interval because it stays within
    [0, 1] and remains reasonable at small n and at p near 0 or 1, both
    of which are real, expected cases here (a company with only 4-8
    historical earnings events on record, or a strategy compatible with
    nearly all or almost none of them). Returns None only when n == 0
    (no sample to build an interval from at all) -- never a fabricated
    range."""
    if n == 0:
        return None
    if successes < 0 or successes > n:
        raise ValueError(f"successes={successes} must be in [0, {n}]")

    n_dec = Decimal(n)
    p_hat = Decimal(successes) / n_dec
    z2 = z * z

    denominator = 1 + z2 / n_dec
    center = (p_hat + z2 / (2 * n_dec)) / denominator
    spread_term = (p_hat * (1 - p_hat) / n_dec) + (z2 / (4 * n_dec * n_dec))
    margin = (z * spread_term.sqrt()) / denominator

    lower = max(Decimal(0), center - margin)
    upper = min(Decimal(1), center + margin)
    return lower, upper


@dataclass(frozen=True)
class EstimatedProbability:
    method: str
    sample_size: int
    compatible_count: int
    probability: Decimal
    low_sample_confidence: bool
    wilson_lower: Decimal | None
    wilson_upper: Decimal | None


def build_estimated_probability(
    move_compatibility: MoveCompatibility | None,
) -> EstimatedProbability | None:
    """Wraps an already-computed MoveCompatibility with a confidence
    interval and small-sample flag -- never recomputes the probability
    itself from a different source. Returns None when MoveCompatibility
    itself is None (no real breakevens or no historical moves on record),
    matching the same "no data -> no result" rule everywhere else in this
    project."""
    if move_compatibility is None:
        return None

    interval = wilson_confidence_interval(
        move_compatibility.compatible_count, move_compatibility.sample_size
    )
    lower, upper = interval if interval is not None else (None, None)

    return EstimatedProbability(
        method="historical_earnings_move_distribution",
        sample_size=move_compatibility.sample_size,
        compatible_count=move_compatibility.compatible_count,
        probability=move_compatibility.compatible_pct,
        low_sample_confidence=move_compatibility.sample_size < LOW_SAMPLE_THRESHOLD,
        wilson_lower=lower,
        wilson_upper=upper,
    )
