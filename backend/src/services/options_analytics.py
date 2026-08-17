"""Computes and persists implied move + ATM IV from ingested OptionsSnapshot
rows for a company's next earnings event. See
analytics/options/implied_move.py for the underlying deterministic
methodology and docs/options_methodology.md for the full writeup.

Mirrors the collect/get split in services/market_expectations.py: writing a
new VolatilitySnapshot is a deliberate point-in-time snapshot (so an implied
move computed at T-7 stays a fixed historical record even if later price or
options data changes), not something recomputed live on every API read.

No options-chain provider currently returns real data for this project's
Alpha Vantage plan (see providers/alpha_vantage_options.py), so
OptionsSnapshot is empty today and every function here honestly returns
None against that empty table rather than fabricating a result. This module
is complete and ready to compute real values the moment ingestion exists.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.options.implied_move import (
    NoQuoteAvailable,
    calculate_atm_iv,
    calculate_atm_straddle_implied_move,
    select_expiration_after,
)
from analytics.options.sentiment import (
    iv_term_structure,
    put_call_open_interest_ratio,
    put_call_volume_ratio,
)
from models.company import Company
from models.earnings_event import EarningsEvent
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.price_reaction import PriceReaction
from models.volatility_snapshot import VolatilitySnapshot
from providers.types import OptionQuote


def _latest_snapshot_timestamp(db: Session, company_id: int) -> datetime | None:
    row = (
        db.query(OptionsSnapshot.snapshot_timestamp)
        .filter(OptionsSnapshot.company_id == company_id)
        .order_by(OptionsSnapshot.snapshot_timestamp.desc())
        .first()
    )
    return row[0] if row else None


def _latest_close_price_on_or_before(db: Session, ticker: str, as_of: date) -> Decimal | None:
    row = (
        db.query(PriceBar)
        .filter(PriceBar.ticker == ticker, PriceBar.trade_date <= as_of)
        .order_by(PriceBar.trade_date.desc())
        .first()
    )
    return row.close if row else None


def _to_option_quote(row: OptionsSnapshot, ticker: str) -> OptionQuote:
    return OptionQuote(
        ticker=ticker,
        snapshot_timestamp=row.snapshot_timestamp,
        expiration_date=row.expiration_date,
        strike=row.strike,
        option_type=row.option_type.value,
        bid=row.bid,
        ask=row.ask,
        last_price=row.last_price,
        volume=row.volume,
        open_interest=row.open_interest,
        implied_volatility=row.implied_volatility,
        delta=row.delta,
        gamma=row.gamma,
        theta=row.theta,
        vega=row.vega,
        source_provider=row.source_provider,
        retrieved_at=row.retrieved_at,
    )


def compute_and_persist_volatility_snapshot(
    db: Session, company: Company, earnings_date: date
) -> VolatilitySnapshot | None:
    """Computes implied move + ATM IV from the most recently ingested
    options-chain snapshot for ``company``, using the nearest expiration
    strictly after ``earnings_date``. Persists and returns a new
    VolatilitySnapshot row.

    Returns None -- never a fabricated result -- when: no options quotes
    have been ingested for this company yet, no expiration after
    ``earnings_date`` is present in the chain, no ATM call+put pair exists
    at the chosen expiration, or no price data exists to determine the
    underlying price as of the options snapshot.
    """
    snapshot_timestamp = _latest_snapshot_timestamp(db, company.id)
    if snapshot_timestamp is None:
        return None

    rows = (
        db.query(OptionsSnapshot)
        .filter(
            OptionsSnapshot.company_id == company.id,
            OptionsSnapshot.snapshot_timestamp == snapshot_timestamp,
        )
        .all()
    )
    if not rows:
        return None

    quotes = [_to_option_quote(r, company.ticker) for r in rows]
    available_expirations = {q.expiration_date for q in quotes}
    expiration = select_expiration_after(available_expirations, earnings_date)
    if expiration is None:
        return None

    underlying_price = _latest_close_price_on_or_before(
        db, company.ticker, snapshot_timestamp.date()
    )
    if underlying_price is None:
        return None

    computed_at = datetime.now(UTC)
    try:
        move = calculate_atm_straddle_implied_move(
            quotes,
            expiration=expiration,
            underlying_price=underlying_price,
            computed_at=computed_at,
        )
    except NoQuoteAvailable:
        return None

    same_expiration = [q for q in quotes if q.expiration_date == expiration]
    call = next(
        q for q in same_expiration if q.strike == move.atm_strike and q.option_type == "call"
    )
    put = next(q for q in same_expiration if q.strike == move.atm_strike and q.option_type == "put")
    iv = calculate_atm_iv(call, put)

    inputs = move.as_inputs_json()
    inputs["atm_iv_method"] = iv.method
    inputs["atm_iv_diverges"] = iv.diverges
    if iv.call_iv is not None:
        inputs["call_iv"] = str(iv.call_iv)
    if iv.put_iv is not None:
        inputs["put_iv"] = str(iv.put_iv)

    # IV term structure and put/call ratios are computed on a best-effort
    # basis -- a chain with only one forward expiration, or with no
    # open_interest/volume reported, still yields a real implied move and
    # ATM IV; these fields just stay null rather than blocking the whole
    # snapshot.
    next_expiration = select_expiration_after(available_expirations, expiration)
    atm_iv_next = None
    term_structure_slope = None
    if next_expiration is not None:
        term_structure = iv_term_structure(quotes, expiration, next_expiration, underlying_price)
        atm_iv_next = term_structure.next_atm_iv
        term_structure_slope = term_structure.slope

    row = VolatilitySnapshot(
        company_id=company.id,
        snapshot_timestamp=snapshot_timestamp,
        method=move.method,
        target_earnings_date=earnings_date,
        near_term_expiration=expiration,
        next_term_expiration=next_expiration,
        atm_iv_near=iv.atm_iv,
        atm_iv_next=atm_iv_next,
        term_structure_slope=term_structure_slope,
        implied_move_pct=move.implied_move_pct,
        implied_move_absolute=move.implied_move_absolute,
        put_call_open_interest_ratio=put_call_open_interest_ratio(quotes),
        put_call_volume_ratio=put_call_volume_ratio(quotes),
        inputs=inputs,
        computed_at=computed_at,
    )
    db.add(row)
    db.commit()
    return row


def has_any_options_data(db: Session) -> bool:
    """Whether any options-chain quote has ever been ingested for any
    company -- the single fact that explains why implied_vs_realized stays
    empty (no provider on this project's Alpha Vantage plan returns real
    options data yet; see providers/alpha_vantage_options.py)."""
    return db.query(OptionsSnapshot.id).first() is not None


def get_latest_volatility_snapshot(db: Session, company_id: int) -> VolatilitySnapshot | None:
    """Most recently computed implied-move/ATM-IV snapshot for a company,
    regardless of which expiration or earnings event it was computed for."""
    return (
        db.query(VolatilitySnapshot)
        .filter(VolatilitySnapshot.company_id == company_id)
        .order_by(VolatilitySnapshot.snapshot_timestamp.desc())
        .first()
    )


@dataclass(frozen=True)
class ImpliedVsRealizedMove:
    target_earnings_date: date
    snapshot_timestamp: datetime
    near_term_expiration: date | None
    implied_move_pct: Decimal | None
    realized_next_day_move_pct: Decimal


def get_implied_vs_realized_moves(db: Session, company_id: int) -> list[ImpliedVsRealizedMove]:
    """Every VolatilitySnapshot computed for ``company_id`` whose target
    earnings date has since been reported with a real next_day_move_pct on
    record -- an implied move that can now be checked against what actually
    happened. Matches purely on (company, date), independent of whether an
    EarningsEvent row existed yet at snapshot time (see
    VolatilitySnapshot.target_earnings_date).

    Empty until options data has been ingested ahead of a real earnings
    date and that date has since been reported -- true for every covered
    company today, since no options-chain provider returns real data on
    this project's current Alpha Vantage plan (see
    providers/alpha_vantage_options.py). This is the "forward accumulation"
    this project relies on: each real snapshot taken between now and a
    future earnings date becomes one more row here once that date reports.
    """
    rows = (
        db.query(VolatilitySnapshot, PriceReaction.next_day_move_pct)
        .join(EarningsEvent, EarningsEvent.company_id == VolatilitySnapshot.company_id)
        .join(PriceReaction, PriceReaction.earnings_event_id == EarningsEvent.id)
        .filter(
            VolatilitySnapshot.company_id == company_id,
            VolatilitySnapshot.target_earnings_date == EarningsEvent.earnings_date,
            PriceReaction.next_day_move_pct.isnot(None),
        )
        .order_by(VolatilitySnapshot.snapshot_timestamp)
        .all()
    )
    return [
        ImpliedVsRealizedMove(
            target_earnings_date=snapshot.target_earnings_date,
            snapshot_timestamp=snapshot.snapshot_timestamp,
            near_term_expiration=snapshot.near_term_expiration,
            implied_move_pct=snapshot.implied_move_pct,
            realized_next_day_move_pct=realized_move_pct,
        )
        for snapshot, realized_move_pct in rows
    ]
