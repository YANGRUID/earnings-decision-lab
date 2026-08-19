"""Loads the real options-chain snapshot for a company -- via the canonical
snapshot-selection policy (Phase 14.10 Part D: current if priceable, else
the most recent stored snapshot that IS priceable) -- and turns it into
deterministic strategy candidates. Pure generation math lives in
analytics/options/strategy_candidates.py; this module's only job is
finding the real data to feed it -- the same snapshot-selection and
expiration/underlying-price rules compute_and_persist_volatility_snapshot
already uses (see services/options_analytics.py), reused directly rather
than re-derived, so strategy candidates and the implied move they're
ranked against can never disagree about which snapshot they came from.
"""

from datetime import date

from sqlalchemy.orm import Session

from analytics.options.implied_move import select_target_expiration_and_anchor
from analytics.options.strategy_candidates import StrategyCandidate, generate_candidates
from models.company import Company
from services.options_analytics import (
    PricingSnapshotSelection,
    _latest_close_price_on_or_before,
    select_pricing_snapshot,
)


def generate_strategy_candidates(
    db: Session,
    company: Company,
    earnings_date: date | None,
    selection: PricingSnapshotSelection | None = None,
) -> list[StrategyCandidate]:
    """Every strategy candidate the selected options-chain snapshot
    supports. When ``earnings_date`` is known, anchored to it (nearest
    expiration strictly after); when ``None``, uses the nearest listed
    expiration on or after the snapshot's own date instead -- the same
    shared rule compute_and_persist_volatility_snapshot uses (see
    select_target_expiration_and_anchor), so the two can never disagree
    about which expiration a given chain represents.

    ``selection`` lets a caller that already ran ``select_pricing_snapshot``
    (e.g. the Strategy Lab endpoint, which also needs it for the state bar)
    pass it in rather than re-querying; when omitted, this function runs
    the selection itself.

    Returns an empty list -- never fabricated data -- when no options
    snapshot has been ingested yet for this company, the selected snapshot
    isn't priceable (contracts-only/none tiers), no matching expiration
    exists in that snapshot's chain, or no underlying price is on record as
    of the snapshot.
    """
    if selection is None:
        selection = select_pricing_snapshot(db, company)
    if not selection.quotes or selection.tier == "contracts_only":
        return []

    quotes = selection.quotes
    snapshot_timestamp = selection.snapshot_timestamp
    assert snapshot_timestamp is not None  # guaranteed non-None whenever quotes is non-empty

    available_expirations = {q.expiration_date for q in quotes}
    expiration, _anchor = select_target_expiration_and_anchor(
        available_expirations, earnings_date, snapshot_timestamp.date()
    )
    if expiration is None:
        return []

    # A reconstructed close snapshot carries its own real, same-window
    # underlying price on every quote (Phase 14.13 Part 9) -- using that
    # keeps strategy candidates coherent with the options data they're
    # actually built from, rather than silently mixing yesterday's
    # reconstructed premiums with whatever the latest daily close bar
    # happens to be.
    underlying_price = quotes[0].underlying_price
    if underlying_price is None:
        underlying_price = _latest_close_price_on_or_before(
            db, company.ticker, snapshot_timestamp.date()
        )
    if underlying_price is None:
        return []

    return generate_candidates(quotes, underlying_price, expiration)
