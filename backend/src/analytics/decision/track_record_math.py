"""Phase 4.6 -- pure formulas for AI Earnings Analyst Track Record
analytics: portfolio performance, prediction accuracy, and probability
calibration. Deliberately data-source-agnostic (no DB, no ORM types) --
callers (services/benchmark_track_record.py) pass in already-queried
plain values; this module only computes.

Reuses services/track_record.py's own ``Rate`` shape (correct/total,
``.pct`` is ``None`` when ``total == 0`` -- "never fabricate a
percentage from zero samples"), but is a wholly separate implementation
bound to Phase 4's own tables. This project's other track-record system
(services/track_record.py, over the legacy AIDecisionVersion journal) is
never imported here, by explicit instruction -- different table,
different real capability (that system explicitly cannot compute a real
win rate; this one can, for the first time in this project).
"""

from dataclasses import dataclass
from decimal import Decimal
from statistics import median


@dataclass(frozen=True)
class Rate:
    """A fraction with its real sample size attached -- never displayed
    without one. ``pct`` is a 0-1 fraction (matching this codebase's own
    convention: fields named ``*_pct`` are 0-100, unsuffixed rate/
    probability values are 0-1), ``None`` only when ``total == 0``."""

    correct: int
    total: int

    @property
    def pct(self) -> Decimal | None:
        if self.total == 0:
            return None
        return Decimal(self.correct) / Decimal(self.total)


def rate_from_bools(values: list[bool]) -> Rate:
    return Rate(correct=sum(1 for v in values if v), total=len(values))


# --------------------------------------------------------------------------
# Portfolio-level performance
# --------------------------------------------------------------------------


def compute_average(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, Decimal(0)) / Decimal(len(values))


def compute_median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return median(values)


def compute_profit_factor(realized_pnls: list[Decimal]) -> Decimal | None:
    """gross profit / gross loss (both real magnitudes). ``None`` when
    the denominator (gross loss) is exactly zero -- either no losses yet
    or no settled decisions at all -- never reported as infinite or as a
    fabricated large number."""
    gross_profit = sum((p for p in realized_pnls if p > 0), Decimal(0))
    gross_loss = sum((-p for p in realized_pnls if p < 0), Decimal(0))
    if gross_loss == 0:
        return None
    return gross_profit / gross_loss


@dataclass(frozen=True)
class DrawdownResult:
    """``None`` for both fields only when there are zero settled
    decisions to build an equity curve from at all -- a real equity
    curve with no drawdown (every trade a winner) is a real ``0``, not
    ``None``; those two cases are never conflated."""

    max_drawdown: Decimal | None
    max_drawdown_pct: Decimal | None


def compute_equity_curve(initial_capital: Decimal, ordered_pnls: list[Decimal]) -> list[Decimal]:
    """Running equity after each settled decision, in the order the
    caller provides (Phase 4.6 approved decision 1: ordered by
    SettlementCaptureAttempt.captured_at, chronological settlement
    order) -- ``equity[i] = initial_capital + sum(pnl[0..i])``."""
    equity: list[Decimal] = []
    running = initial_capital
    for pnl in ordered_pnls:
        running += pnl
        equity.append(running)
    return equity


def compute_max_drawdown(initial_capital: Decimal, ordered_pnls: list[Decimal]) -> DrawdownResult:
    """Real dollar equity curve against the fixed benchmark starting
    capital, never an R-multiple curve (Phase 4.6 approved decision 1,
    explicit: "Do not calculate drawdown from R multiples")."""
    if not ordered_pnls:
        return DrawdownResult(max_drawdown=None, max_drawdown_pct=None)

    equity = compute_equity_curve(initial_capital, ordered_pnls)
    peak = initial_capital
    max_dd = Decimal(0)
    max_dd_pct = Decimal(0)
    for point in equity:
        if point > peak:
            peak = point
        drawdown = peak - point
        if drawdown > max_dd:
            max_dd = drawdown
            max_dd_pct = (drawdown / peak * 100) if peak > 0 else Decimal(0)
    return DrawdownResult(max_drawdown=max_dd, max_drawdown_pct=max_dd_pct)


# --------------------------------------------------------------------------
# DTE buckets (Phase 4.6 approved decision 3: expiration_date - earnings_date,
# not decision generation date)
# --------------------------------------------------------------------------

# Contiguous, non-overlapping by construction: each bucket's lower bound
# is the previous bucket's upper bound + 1. The last bucket has no upper
# bound (None) but is labeled "30+" per the literal instruction, even
# though its real boundary is DTE >= 31 (see this module's own docstring
# and the architecture review's 2026-08-21 addendum item 3 for why 30
# itself belongs to "15-30", not "30+").
DTE_BUCKETS: list[tuple[str, int, int | None]] = [
    ("0-3", 0, 3),
    ("4-7", 4, 7),
    ("8-14", 8, 14),
    ("15-30", 15, 30),
    ("30+", 31, None),
]


def dte_bucket_label(dte: int) -> str | None:
    """``None`` only for a negative DTE, which should never occur (the
    Expiration Engine always selects an expiration strictly after the
    earnings date -- see analytics/options/expiration_selection.py) --
    never guessed into a bucket it doesn't belong in."""
    if dte < 0:
        return None
    for label, lower, upper in DTE_BUCKETS:
        if upper is None:
            if dte >= lower:
                return label
        elif lower <= dte <= upper:
            return label
    return None


# --------------------------------------------------------------------------
# Probability / confidence buckets (Phase 4.6 approved decision 5: five
# buckets, <60% included)
# --------------------------------------------------------------------------

# Half-open intervals, contiguous and non-overlapping: [None, 60), [60,
# 70), [70, 80), [80, 90), [90, None). Compared against a 0-100 percent
# value (estimated_probability * 100), matching this bucket scheme's own
# literal "<60%"/"90%+" labels.
PROBABILITY_BUCKETS: list[tuple[str, int | None, int | None]] = [
    ("<60%", None, 60),
    ("60-70%", 60, 70),
    ("70-80%", 70, 80),
    ("80-90%", 80, 90),
    ("90%+", 90, None),
]


def probability_bucket_label(probability: Decimal) -> str:
    """``probability`` is the raw 0-1 fraction (DecisionSnapshot.
    estimated_probability's own stored scale -- see analytics/decision/
    probability.py::build_estimated_probability, which sources it
    directly from MoveCompatibility.compatible_pct, itself a plain
    count/sample_size fraction). Converted to a 0-100 percent here, once,
    for bucketing -- never assumed to already be on a 0-100 scale."""
    pct = probability * 100
    for label, lower, upper in PROBABILITY_BUCKETS:
        if lower is None:
            if pct < upper:  # type: ignore[operator]
                return label
        elif upper is None:
            if pct >= lower:
                return label
        elif lower <= pct < upper:
            return label
    # Unreachable for any real 0-1 probability, but never silently
    # misbucket a value the ranges above didn't account for.
    return "90%+"
