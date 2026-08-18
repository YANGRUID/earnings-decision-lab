"""Queries real, already-persisted PriceReaction rows to summarize how a
company has historically moved the day after reporting earnings. See
analytics/earnings/historical_moves.py for the pure statistics.
"""

from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.earnings.historical_moves import HistoricalMoveStats, historical_move_stats
from models.earnings_event import EarningsEvent
from models.price_reaction import PriceReaction


def get_historical_move_pcts(
    db: Session, company_id: int, exclude_event_id: int | None = None
) -> list[Decimal]:
    """Every real next_day_move_pct on record for ``company_id``'s reported
    earnings events -- the raw values behind get_historical_move_stats,
    for callers (e.g. analytics/options/move_compatibility.py) that need
    the individual moves rather than a summary. ``exclude_event_id`` keeps
    the event currently being viewed from appearing in its own historical
    baseline; omit it for a company-wide list.
    """
    query = (
        db.query(PriceReaction.next_day_move_pct)
        .join(EarningsEvent, PriceReaction.earnings_event_id == EarningsEvent.id)
        .filter(
            EarningsEvent.company_id == company_id,
            PriceReaction.next_day_move_pct.isnot(None),
        )
    )
    if exclude_event_id is not None:
        query = query.filter(EarningsEvent.id != exclude_event_id)
    return [r[0] for r in query.all()]


def get_historical_move_stats(
    db: Session, company_id: int, exclude_event_id: int | None = None
) -> HistoricalMoveStats | None:
    """Statistics over reported earnings events for ``company_id`` that
    have a real next_day_move_pct on record. ``exclude_event_id`` keeps the
    event currently being viewed from appearing in its own historical
    baseline (the Earnings Event page's use case); omit it entirely for a
    company-wide summary that isn't anchored to any single event (the
    Historical Replay page's use case).
    """
    moves = get_historical_move_pcts(db, company_id, exclude_event_id)
    return historical_move_stats(moves)
