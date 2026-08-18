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

from analytics.options.implied_move import select_expiration_after
from analytics.options.strategy_candidates import StrategyCandidate, generate_candidates
from models.company import Company
from models.options_snapshot import OptionsSnapshot
from services.options_analytics import (
    _latest_close_price_on_or_before,
    _latest_snapshot_timestamp,
    _to_option_quote,
)


def generate_strategy_candidates(
    db: Session, company: Company, earnings_date: date
) -> list[StrategyCandidate]:
    """Every strategy candidate the most recently ingested options-chain
    snapshot supports for ``company``'s upcoming ``earnings_date``.

    Returns an empty list -- never fabricated data -- when no options
    snapshot has been ingested yet for this company, no expiration after
    ``earnings_date`` exists in that snapshot's chain, or no underlying
    price is on record as of the snapshot.
    """
    snapshot_timestamp = _latest_snapshot_timestamp(db, company.id)
    if snapshot_timestamp is None:
        return []

    rows = (
        db.query(OptionsSnapshot)
        .filter(
            OptionsSnapshot.company_id == company.id,
            OptionsSnapshot.snapshot_timestamp == snapshot_timestamp,
        )
        .all()
    )
    if not rows:
        return []

    quotes = [_to_option_quote(r, company.ticker) for r in rows]
    available_expirations = {q.expiration_date for q in quotes}
    expiration = select_expiration_after(available_expirations, earnings_date)
    if expiration is None:
        return []

    underlying_price = _latest_close_price_on_or_before(
        db, company.ticker, snapshot_timestamp.date()
    )
    if underlying_price is None:
        return []

    return generate_candidates(quotes, underlying_price, expiration)
