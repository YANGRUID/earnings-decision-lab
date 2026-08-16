"""Backfill real earnings_date values from 8-K Item 2.02 filings.

SEC's Item 2.02 ("Results of Operations and Financial Condition") is the
reliable signal that an 8-K is an earnings-release filing — see SEC EDGAR's
own item-code documentation. This is a sourced fact, not a guess, unlike
using a 10-Q/10-K filing date (which trails the actual release by weeks).

Matching an event to a specific 8-K still requires a temporal-proximity
rule (the 8-K JSON doesn't declare which fiscal quarter it reports on), so
the match itself is a documented heuristic: the nearest qualifying 8-K filed
10-60 days after the quarter's period_end_date, each 8-K used at most once.
"""

from dataclasses import dataclass
from datetime import date

MIN_LAG_DAYS = 10
MAX_LAG_DAYS = 60


@dataclass(frozen=True)
class EventPeriod:
    key: str
    period_end_date: date


@dataclass(frozen=True)
class Candidate8K:
    accession_number: str
    filing_date: date
    items: str | None


def is_earnings_release_8k(items: str | None) -> bool:
    if not items:
        return False
    return "2.02" in [code.strip() for code in items.split(",")]


def match_earnings_dates(
    events: list[EventPeriod], filings: list[Candidate8K]
) -> dict[str, Candidate8K]:
    """Best-effort match of each event to the 8-K that announced it.

    Returns {event.key: matched_filing} — events with no qualifying 8-K in
    the lag window are simply absent from the result (left for a real
    earnings-calendar provider, not guessed at).
    """
    qualifying = [f for f in filings if is_earnings_release_8k(f.items)]
    used_accessions: set[str] = set()
    matches: dict[str, Candidate8K] = {}

    for event in sorted(events, key=lambda e: e.period_end_date):
        in_window = [
            f
            for f in qualifying
            if f.accession_number not in used_accessions
            and MIN_LAG_DAYS <= (f.filing_date - event.period_end_date).days <= MAX_LAG_DAYS
        ]
        if not in_window:
            continue
        best = min(in_window, key=lambda f: (f.filing_date - event.period_end_date).days)
        matches[event.key] = best
        used_accessions.add(best.accession_number)

    return matches
