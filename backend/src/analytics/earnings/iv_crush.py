"""IV crush and implied-vs-realised comparison — deterministic, pure
functions over already-collected snapshots. No LLM involvement.

These operate on plain (Decimal) inputs, not ORM objects, so they can be
unit-tested without a database and reused identically whether the caller
sourced pre/post IV from real options-chain data or (in tests) fixtures.
Currently there is no real historical options-chain provider wired up (see
docs/data_sources.md), so nothing calls these against real data yet — see
docs/earnings_methodology.md.
"""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class IVCrushResult:
    pre_event_iv: Decimal
    post_event_iv: Decimal
    absolute_change: Decimal
    relative_crush_pct: Decimal  # negative = IV fell (the typical post-earnings case)


def calculate_iv_crush(pre_event_iv: Decimal, post_event_iv: Decimal) -> IVCrushResult:
    if pre_event_iv <= 0:
        raise ValueError("pre_event_iv must be positive")
    absolute_change = post_event_iv - pre_event_iv
    relative_crush_pct = absolute_change / pre_event_iv
    return IVCrushResult(
        pre_event_iv=pre_event_iv,
        post_event_iv=post_event_iv,
        absolute_change=absolute_change,
        relative_crush_pct=relative_crush_pct,
    )


@dataclass(frozen=True)
class ImpliedVsRealisedResult:
    implied_move_pct: Decimal
    realised_move_pct: Decimal
    error: Decimal  # realised - implied; positive = market underpriced the move
    verdict: str  # "underpriced" | "overpriced" | "accurate"


_ACCURATE_THRESHOLD = Decimal("0.005")  # within 0.5 pts counts as "accurate", not a real edge


def compare_implied_vs_realised(
    implied_move_pct: Decimal, realised_move_pct: Decimal
) -> ImpliedVsRealisedResult:
    """``realised_move_pct`` should be an absolute-value move (e.g. from
    ``abs(price_reaction.next_day_move_pct)``) to compare against the
    (inherently non-directional) straddle-implied move.
    """
    error = realised_move_pct - implied_move_pct
    if abs(error) <= _ACCURATE_THRESHOLD:
        verdict = "accurate"
    elif error > 0:
        verdict = "underpriced"  # market's implied move was too small
    else:
        verdict = "overpriced"  # market's implied move was too large
    return ImpliedVsRealisedResult(
        implied_move_pct=implied_move_pct,
        realised_move_pct=realised_move_pct,
        error=error,
        verdict=verdict,
    )


@dataclass(frozen=True)
class HistoryRecord:
    """One event's worth of implied-vs-realised + IV-crush data, as input to
    the summary functions below."""

    implied_move_pct: Decimal
    realised_move_pct: Decimal
    pre_event_iv: Decimal | None = None
    post_event_iv: Decimal | None = None


@dataclass(frozen=True)
class HistorySummary:
    event_count: int
    average_implied_move_pct: Decimal
    average_realised_move_pct: Decimal
    average_error: Decimal
    underpriced_count: int
    overpriced_count: int
    accurate_count: int
    average_iv_crush_pct: Decimal | None


def summarize_history(records: list[HistoryRecord]) -> HistorySummary:
    if not records:
        raise ValueError("at least one record is required")

    comparisons = [
        compare_implied_vs_realised(r.implied_move_pct, r.realised_move_pct) for r in records
    ]
    n = len(records)
    crush_values = [
        calculate_iv_crush(r.pre_event_iv, r.post_event_iv).relative_crush_pct
        for r in records
        if r.pre_event_iv is not None and r.post_event_iv is not None
    ]

    return HistorySummary(
        event_count=n,
        average_implied_move_pct=sum((r.implied_move_pct for r in records), Decimal(0)) / n,
        average_realised_move_pct=sum((r.realised_move_pct for r in records), Decimal(0)) / n,
        average_error=sum((c.error for c in comparisons), Decimal(0)) / n,
        underpriced_count=sum(1 for c in comparisons if c.verdict == "underpriced"),
        overpriced_count=sum(1 for c in comparisons if c.verdict == "overpriced"),
        accurate_count=sum(1 for c in comparisons if c.verdict == "accurate"),
        average_iv_crush_pct=(sum(crush_values, Decimal(0)) / len(crush_values))
        if crush_values
        else None,
    )
