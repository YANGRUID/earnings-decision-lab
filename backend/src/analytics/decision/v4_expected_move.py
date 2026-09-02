"""Options Decision Engine V4.3 -- Expected-Move Context (2026-09-02).

ONE authoritative expected-move object every V4.3 strike-construction
function reads from -- never a second, ad hoc expected-move formula
scattered across strategy builders (this task's own Section 3). Reuses
the existing, already-tested V3 methodology functions rather than
inventing a new one (Section 3's own instruction):
analytics.options.implied_move.calculate_atm_straddle_implied_move for
the market-implied signal, analytics.earnings.historical_moves
.historical_move_stats for the historical signal. Neither of those
modules is modified here.

IMPLIED MOVE and HISTORICAL MOVE DISTRIBUTION are kept as explicitly
SEPARATE fields (this task's own Section 4) -- what current option
prices imply is a different signal from what the stock has actually
done around prior earnings, and this object never averages or blends
them into one number. Downstream V4.3 strike construction chooses
between them by an explicit, documented preference order per use case
(see v4_strike_engine.py's own boundary-selection helpers) -- it never
fits a blend to the 7 real settled trades (Section 27's anti-fitting
rule).

Pure function, no DB access: callers fetch the real quotes/history and
pass them in. V3 does not depend on this module.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from analytics.earnings.historical_moves import HistoricalMoveStats, historical_move_stats
from analytics.options.implied_move import (
    ImpliedMoveResult,
    NoQuoteAvailable,
    calculate_atm_straddle_implied_move,
)
from providers.types import OptionQuote

EXPECTED_MOVE_CONTEXT_VERSION = "expected_move_context_v1"

# Evidence-quality gates for historical earnings-move statistics (this
# task's own Sections 16/17: "do not pretend N=4 supports robust tail
# percentiles"). Fixed, named thresholds chosen from ordinary statistical
# common sense -- a quartile is not meaningful below 4 real observations,
# a decile not meaningful below 10 -- never fitted against the 7 real
# settled trades this task explicitly forbids fitting against.
MIN_N_FOR_MEDIAN = 1
MIN_N_FOR_QUARTILES = 4
MIN_N_FOR_DECILES = 10

HistoricalEvidenceQuality = Literal[
    "insufficient", "limited", "adequate_quartiles", "adequate_deciles"
]


def _quantile(sorted_values: list[Decimal], q: Decimal) -> Decimal:
    """Linear-interpolation quantile over an already-sorted list (the
    standard "type 7" method -- numpy's default, Excel's
    PERCENTILE.INC) -- interpolated only WITHIN the real observed
    range, never extrapolated beyond it."""
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    pos = q * (n - 1)
    lower_idx = int(pos)
    upper_idx = min(lower_idx + 1, n - 1)
    frac = pos - lower_idx
    return sorted_values[lower_idx] + (sorted_values[upper_idx] - sorted_values[lower_idx]) * frac


@dataclass(frozen=True)
class HistoricalMoveQuantiles:
    p25_abs_move_pct: Decimal
    p75_abs_move_pct: Decimal
    # None below MIN_N_FOR_DECILES -- never a false-precision estimate
    # from too small a sample (Section 16's explicit instruction).
    p10_abs_move_pct: Decimal | None
    p90_abs_move_pct: Decimal | None
    sample_n: int


def _compute_quantiles(abs_moves: list[Decimal]) -> HistoricalMoveQuantiles | None:
    n = len(abs_moves)
    if n < MIN_N_FOR_QUARTILES:
        return None
    ordered = sorted(abs_moves)
    p10 = _quantile(ordered, Decimal("0.10")) if n >= MIN_N_FOR_DECILES else None
    p90 = _quantile(ordered, Decimal("0.90")) if n >= MIN_N_FOR_DECILES else None
    return HistoricalMoveQuantiles(
        p25_abs_move_pct=_quantile(ordered, Decimal("0.25")),
        p75_abs_move_pct=_quantile(ordered, Decimal("0.75")),
        p10_abs_move_pct=p10,
        p90_abs_move_pct=p90,
        sample_n=n,
    )


def _historical_evidence_quality(n: int) -> HistoricalEvidenceQuality:
    if n < MIN_N_FOR_MEDIAN:
        return "insufficient"
    if n < MIN_N_FOR_QUARTILES:
        return "limited"
    if n < MIN_N_FOR_DECILES:
        return "adequate_quartiles"
    return "adequate_deciles"


@dataclass(frozen=True)
class ExpectedMoveContext:
    """The single object every V4.3 strategy builder consumes. Every
    "unavailable" field is a real, honest absence -- never fabricated
    (Section 2: "Do not fabricate unavailable inputs")."""

    spot: Decimal
    observed_at: datetime

    implied_move_available: bool
    implied_move_dollars: Decimal | None
    implied_move_pct: Decimal | None
    upper_implied_boundary: Decimal | None
    lower_implied_boundary: Decimal | None
    implied_move_source: Literal["atm_straddle", "unavailable"]
    implied_move_result: ImpliedMoveResult | None

    historical_sample_n: int
    historical_evidence_quality: HistoricalEvidenceQuality
    historical_median_abs_move_pct: Decimal | None
    historical_median_upper_boundary: Decimal | None
    historical_median_lower_boundary: Decimal | None
    historical_quantiles: HistoricalMoveQuantiles | None
    historical_move_stats: HistoricalMoveStats | None

    context_version: str


def derive_expected_move_context(
    *,
    spot: Decimal,
    observed_at: datetime,
    expiration: date | None,
    quotes_for_expiration: list[OptionQuote] | None,
    historical_next_day_move_pcts: list[Decimal] | None,
) -> ExpectedMoveContext:
    """``quotes_for_expiration`` should be every real call+put quote
    already fetched for one expiration (matches
    calculate_atm_straddle_implied_move's own contract).
    ``historical_next_day_move_pcts`` should be one company's own real,
    already-persisted PriceReaction.next_day_move_pct values. Neither
    input is required -- both degrade honestly (implied_move_available
    =False / historical_evidence_quality="insufficient") rather than
    raising or guessing."""
    implied: ImpliedMoveResult | None = None
    if quotes_for_expiration and expiration is not None:
        try:
            implied = calculate_atm_straddle_implied_move(
                quotes_for_expiration, expiration, spot, observed_at
            )
        except NoQuoteAvailable:
            implied = None

    if implied is not None:
        implied_move_dollars = implied.implied_move_absolute
        implied_move_pct = implied.implied_move_pct
        upper_boundary = spot + implied_move_dollars
        lower_boundary = spot - implied_move_dollars
        implied_source: Literal["atm_straddle", "unavailable"] = "atm_straddle"
    else:
        implied_move_dollars = None
        implied_move_pct = None
        upper_boundary = None
        lower_boundary = None
        implied_source = "unavailable"

    raw_moves = historical_next_day_move_pcts or []
    hist_stats = historical_move_stats(raw_moves)
    sample_n = hist_stats.sample_size if hist_stats else 0
    quantiles = _compute_quantiles([abs(m) for m in raw_moves]) if hist_stats else None
    quality = _historical_evidence_quality(sample_n)

    median_pct = hist_stats.median_abs_move_pct if hist_stats else None
    hist_upper = spot * (1 + median_pct) if median_pct is not None else None
    hist_lower = spot * (1 - median_pct) if median_pct is not None else None

    return ExpectedMoveContext(
        spot=spot,
        observed_at=observed_at,
        implied_move_available=implied is not None,
        implied_move_dollars=implied_move_dollars,
        implied_move_pct=implied_move_pct,
        upper_implied_boundary=upper_boundary,
        lower_implied_boundary=lower_boundary,
        implied_move_source=implied_source,
        implied_move_result=implied,
        historical_sample_n=sample_n,
        historical_evidence_quality=quality,
        historical_median_abs_move_pct=median_pct,
        historical_median_upper_boundary=hist_upper,
        historical_median_lower_boundary=hist_lower,
        historical_quantiles=quantiles,
        historical_move_stats=hist_stats,
        context_version=EXPECTED_MOVE_CONTEXT_VERSION,
    )
