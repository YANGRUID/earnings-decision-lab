"""Deterministic forward options-snapshot collection schedule: exactly
T-14, T-7, T-3, T-1 trading-day-agnostic calendar-day offsets before a real
earnings date, never a fuzzy "close enough" window. Collecting only ahead
of a real, already-known earnings date -- never after it, never using data
that couldn't have been observed at the time -- is what keeps this
scheduler free of lookahead bias; see docs/engineering_decisions.md.
"""

from datetime import date

DEFAULT_COLLECTION_OFFSETS: tuple[int, ...] = (14, 7, 3, 1)


def should_collect_snapshot(
    earnings_date: date,
    today: date,
    offsets: tuple[int, ...] = DEFAULT_COLLECTION_OFFSETS,
) -> bool:
    """True only on the exact calendar days ``offsets`` days before
    ``earnings_date`` -- e.g. exactly 14, 7, 3, and 1 days before by
    default. False on the earnings date itself or after it: a snapshot
    collector's job is to observe the market's forward-looking expectation
    *before* the event, never to look back at it.
    """
    days_until_earnings = (earnings_date - today).days
    return days_until_earnings in offsets
