"""Loads the most recent real options-chain snapshot for a company and
turns it into deterministic strategy candidates (Phase 14). Pure
generation math lives in analytics/options/strategy_candidates.py; this
module's only job is finding the real data to feed it -- the same
snapshot, expiration, and underlying-price selection rules
compute_and_persist_volatility_snapshot already uses (see
services/options_analytics.py), reused directly rather than re-derived.
"""

from datetime import date

from sqlalchemy.orm import Session

from analytics.options.implied_move import select_target_expiration_and_anchor
from analytics.options.strategy_candidates import StrategyCandidate, generate_candidates
from models.company import Company
from services.options_analytics import (
    _latest_close_price_on_or_before,
    _latest_snapshot_timestamp,
    get_latest_options_chain,
)


def generate_strategy_candidates(
    db: Session, company: Company, earnings_date: date | None
) -> list[StrategyCandidate]:
    """Every strategy candidate the most recently ingested options-chain
    snapshot supports. When ``earnings_date`` is known, anchored to it
    (nearest expiration strictly after); when ``None``, uses the nearest
    listed expiration on or after the snapshot's own date instead -- the
    same shared rule compute_and_persist_volatility_snapshot uses (see
    select_target_expiration_and_anchor), so the two can never disagree
    about which expiration a given chain represents.

    Returns an empty list -- never fabricated data -- when no options
    snapshot has been ingested yet for this company, no matching expiration
    exists in that snapshot's chain, or no underlying price is on record as
    of the snapshot.
    """
    snapshot_timestamp = _latest_snapshot_timestamp(db, company.id)
    if snapshot_timestamp is None:
        return []

    quotes = get_latest_options_chain(db, company)
    if not quotes:
        return []

    available_expirations = {q.expiration_date for q in quotes}
    expiration, _anchor = select_target_expiration_and_anchor(
        available_expirations, earnings_date, snapshot_timestamp.date()
    )
    if expiration is None:
        return []

    underlying_price = _latest_close_price_on_or_before(
        db, company.ticker, snapshot_timestamp.date()
    )
    if underlying_price is None:
        return []

    return generate_candidates(quotes, underlying_price, expiration)
