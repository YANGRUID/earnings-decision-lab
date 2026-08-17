"""Deterministic historical price-move statistics, built entirely on
next_day_move_pct values already computed by
analytics.earnings.price_moves and persisted on PriceReaction rows — no new
data source needed. Move magnitude (absolute value) is what's comparable to
an implied move (always a magnitude, never signed), so the average and
median are computed over |move|; the largest entry's own sign is kept
separately since "which direction did the biggest move go" is real
information worth reporting alongside the magnitude.
"""

from dataclasses import dataclass
from decimal import Decimal
from statistics import median


@dataclass(frozen=True)
class HistoricalMoveStats:
    sample_size: int
    average_abs_move_pct: Decimal
    median_abs_move_pct: Decimal
    largest_abs_move_pct: Decimal
    largest_move_pct_signed: Decimal


def historical_move_stats(next_day_move_pcts: list[Decimal]) -> HistoricalMoveStats | None:
    """``next_day_move_pcts`` should already be real, persisted
    PriceReaction.next_day_move_pct values for a single company's past
    reported earnings events. Returns None when there's no history yet to
    summarize — never a fabricated zero.
    """
    if not next_day_move_pcts:
        return None

    abs_moves = [abs(m) for m in next_day_move_pcts]
    largest_abs = max(abs_moves)
    largest_signed = next(m for m in next_day_move_pcts if abs(m) == largest_abs)

    return HistoricalMoveStats(
        sample_size=len(next_day_move_pcts),
        average_abs_move_pct=sum(abs_moves, Decimal(0)) / len(abs_moves),
        median_abs_move_pct=median(abs_moves),
        largest_abs_move_pct=largest_abs,
        largest_move_pct_signed=largest_signed,
    )
