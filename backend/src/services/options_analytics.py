"""Computes and persists implied move + ATM IV from ingested OptionsSnapshot
rows for a company's next earnings event. See
analytics/options/implied_move.py for the underlying deterministic
methodology and docs/options_methodology.md for the full writeup.

Mirrors the collect/get split in services/market_expectations.py: writing a
new VolatilitySnapshot is a deliberate point-in-time snapshot (so an implied
move computed at T-7 stays a fixed historical record even if later price or
options data changes), not something recomputed live on every API read.

Works against whichever OptionsDataProvider is configured (see
providers/factory.py) -- Alpha Vantage's REALTIME_OPTIONS remains
premium-gated on this project's plan (providers/alpha_vantage_options.py),
but the real Interactive Brokers Client Portal Gateway adapter
(providers/ibkr_options.py, Phase 13) can populate OptionsSnapshot for
real, locally, when the user's own Gateway is running and authenticated.
Every function here still honestly returns None/empty against an empty
table rather than fabricating a result, regardless of which provider (or
none) is configured.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.data_state import compute_options_data_state, compute_snapshot_age
from analytics.options.collection_schedule import (
    DEFAULT_COLLECTION_OFFSETS,
    should_collect_snapshot,
)
from analytics.options.implied_move import (
    NoQuoteAvailable,
    calculate_atm_iv,
    calculate_atm_straddle_implied_move,
    select_expiration_after,
    select_target_expiration_and_anchor,
)
from analytics.options.sentiment import (
    iv_term_structure,
    put_call_open_interest_ratio,
    put_call_volume_ratio,
)
from models.company import Company
from models.earnings_event import EarningsEvent
from models.enums import DataState, MarketDataQuality, OptionsSnapshotAnchor, OptionType
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.price_reaction import PriceReaction
from models.volatility_snapshot import VolatilitySnapshot
from providers.base import OptionsDataProvider
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
        market_data_quality=row.market_data_quality.value if row.market_data_quality else None,
        external_contract_id=row.external_contract_id,
        source_provider=row.source_provider,
        retrieved_at=row.retrieved_at,
    )


def compute_and_persist_volatility_snapshot(
    db: Session, company: Company, earnings_date: date | None
) -> VolatilitySnapshot | None:
    """Computes implied move + ATM IV from the most recently ingested
    options-chain snapshot for ``company``. When ``earnings_date`` is known,
    uses the nearest expiration strictly after it (earnings-anchored);
    when it's ``None``, uses the nearest expiration on or after the
    snapshot's own date instead (general/current -- see
    select_target_expiration_and_anchor). Persists and returns a new
    VolatilitySnapshot row, labeled with which of the two this was.

    Returns None -- never a fabricated result -- when: no options quotes
    have been ingested for this company yet, no matching expiration is
    present in the chain, no ATM call+put pair exists at the chosen
    expiration, or no price data exists to determine the underlying price
    as of the options snapshot.
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
    expiration, anchor = select_target_expiration_and_anchor(
        available_expirations, earnings_date, snapshot_timestamp.date()
    )
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
        anchor=anchor,
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


def _already_collected_today(db: Session, company_id: int, today: date) -> bool:
    start_of_day = datetime.combine(today, datetime.min.time(), tzinfo=UTC)
    end_of_day = start_of_day + timedelta(days=1)
    return (
        db.query(OptionsSnapshot.id)
        .filter(
            OptionsSnapshot.company_id == company_id,
            OptionsSnapshot.snapshot_timestamp >= start_of_day,
            OptionsSnapshot.snapshot_timestamp < end_of_day,
        )
        .first()
        is not None
    )


def _fetch_and_persist_options_snapshot(
    db: Session,
    provider: OptionsDataProvider,
    company: Company,
    earnings_date: date | None,
    as_of: datetime,
) -> list[OptionsSnapshot]:
    # reference_date lets a bounded provider (e.g. IBKR, which can't return
    # a full chain -- see providers/ibkr_options.py) pick a sensible
    # expiration/strike window; providers that already return everything
    # (e.g. Alpha Vantage) ignore it. When earnings_date is known, it's the
    # real earnings date this snapshot is being collected ahead of, never a
    # guess; when it's None, the provider falls back to "now" and picks the
    # nearest listed expiration instead of pretending one is tied to an
    # earnings date that doesn't exist yet -- see
    # IBKROptionsProvider.get_option_chain's earnings_anchored parameter.
    anchor = (
        OptionsSnapshotAnchor.EARNINGS_ANCHORED
        if earnings_date is not None
        else OptionsSnapshotAnchor.GENERAL_CURRENT
    )
    quotes = provider.get_option_chain(
        company.ticker,
        as_of,
        reference_date=earnings_date,
        earnings_anchored=earnings_date is not None,
    )

    rows = [
        OptionsSnapshot(
            company_id=company.id,
            snapshot_timestamp=as_of,
            expiration_date=quote.expiration_date,
            strike=quote.strike,
            option_type=OptionType(quote.option_type),
            bid=quote.bid,
            ask=quote.ask,
            last_price=quote.last_price,
            volume=quote.volume,
            open_interest=quote.open_interest,
            implied_volatility=quote.implied_volatility,
            delta=quote.delta,
            gamma=quote.gamma,
            theta=quote.theta,
            vega=quote.vega,
            market_data_quality=MarketDataQuality(quote.market_data_quality)
            if quote.market_data_quality
            else None,
            external_contract_id=quote.external_contract_id,
            source_provider=quote.source_provider,
            retrieved_at=quote.retrieved_at,
            anchor=anchor,
        )
        for quote in quotes
    ]
    db.add_all(rows)
    db.commit()
    return rows


def collect_forward_options_snapshot(
    db: Session,
    provider: OptionsDataProvider,
    company: Company,
    earnings_date: date,
    as_of: datetime,
    collection_offsets: tuple[int, ...] = DEFAULT_COLLECTION_OFFSETS,
) -> list[OptionsSnapshot] | None:
    """Fetches and persists a real options-chain snapshot for ``company``
    ahead of ``earnings_date``, but only when ``as_of``'s date is exactly
    one of ``collection_offsets`` calendar days before it (see
    analytics/options/collection_schedule.py) -- this is what makes the T-14
    /T-7/T-3/T-1 schedule real rather than "whenever this happens to run".
    ``as_of`` is the single source of truth for "now": both the schedule
    check and the persisted snapshot_timestamp derive from it, so a caller
    can never end up scheduling against one clock and stamping data with
    another. Used by the daily collection cron (see
    ingestion/collect_options_snapshots.py); on-demand research preparation
    uses ``collect_options_snapshot_now`` instead, which isn't schedule-gated.

    Returns None -- fetching nothing, spending no API quota -- when today
    isn't a scheduled collection day, or when a snapshot was already
    collected for this company today (safe to call more than once a day,
    e.g. if a scheduled run is retried). The caller is responsible for
    letting the provider's own errors (e.g. PremiumEndpointRequiredError)
    propagate -- this function never silently swallows a real fetch
    failure.
    """
    today = as_of.date()
    if not should_collect_snapshot(earnings_date, today, collection_offsets):
        return None
    if _already_collected_today(db, company.id, today):
        return None
    return _fetch_and_persist_options_snapshot(db, provider, company, earnings_date, as_of)


def collect_options_snapshot_now(
    db: Session,
    provider: OptionsDataProvider,
    company: Company,
    earnings_date: date | None,
    as_of: datetime,
) -> list[OptionsSnapshot] | None:
    """Fetches and persists a real options-chain snapshot for ``company``
    right now, regardless of the T-14/T-7/T-3/T-1 cron schedule -- for the
    on-demand research-preparation pipeline (services/research_orchestration.py),
    where the user's own request *is* the trigger, not a daily calendar
    check. Still refuses to double-collect the same day (same dedup as
    ``collect_forward_options_snapshot``), since a user re-preparing the
    same ticker twice in one day shouldn't duplicate point-in-time
    snapshots; the freshness-policy layer (analytics/research/freshness.py)
    is what actually decides whether this gets called again.

    ``earnings_date`` is ``None`` when no reliable upcoming earnings date is
    on record -- collection still proceeds (never skipped just because
    Alpha Vantage hasn't published a date yet), producing a general/current
    snapshot instead of an earnings-anchored one. See
    _fetch_and_persist_options_snapshot and
    IBKROptionsProvider.get_option_chain's earnings_anchored parameter.
    """
    today = as_of.date()
    if _already_collected_today(db, company.id, today):
        return None
    return _fetch_and_persist_options_snapshot(db, provider, company, earnings_date, as_of)


def has_any_options_data(db: Session) -> bool:
    """Whether any options-chain quote has ever been ingested for any
    company -- the single fact that explains why implied_vs_realized stays
    empty (no provider on this project's Alpha Vantage plan returns real
    options data yet; see providers/alpha_vantage_options.py)."""
    return db.query(OptionsSnapshot.id).first() is not None


def get_latest_options_chain(db: Session, company: Company) -> list[OptionQuote]:
    """Every real quote from the most recently ingested options-chain
    snapshot for ``company``, across every expiration in that snapshot --
    the same snapshot selection rule compute_and_persist_volatility_snapshot
    and services/strategy_generation.py already use. Empty (never
    fabricated) when no snapshot has been ingested yet."""
    snapshot_timestamp = _latest_snapshot_timestamp(db, company.id)
    if snapshot_timestamp is None:
        return []
    rows = (
        db.query(OptionsSnapshot)
        .filter(
            OptionsSnapshot.company_id == company.id,
            OptionsSnapshot.snapshot_timestamp == snapshot_timestamp,
        )
        .order_by(OptionsSnapshot.expiration_date, OptionsSnapshot.strike)
        .all()
    )
    return [_to_option_quote(r, company.ticker) for r in rows]


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
class OptionsSnapshotState:
    """The shared "what's the options data right now" summary shown on both
    the Strategy Lab state bar and the Company Overview -- see
    analytics/data_state.py for what each DataState means."""

    data_state: DataState
    snapshot_source: str | None
    snapshot_timestamp: datetime | None
    snapshot_age_minutes: int | None
    snapshot_age_label: str | None


def options_state_from_chain(raw_chain: list[OptionQuote], as_of: datetime) -> OptionsSnapshotState:
    """Pure function over an already-fetched ``get_latest_options_chain()``
    result -- callers that already hold the chain (e.g. to also render its
    quotes) pass it in rather than triggering a second query."""
    snapshot_timestamp = raw_chain[0].snapshot_timestamp if raw_chain else None
    snapshot_source = raw_chain[0].source_provider if raw_chain else None
    snapshot_quality = raw_chain[0].market_data_quality if raw_chain else None
    data_state = compute_options_data_state(snapshot_timestamp, snapshot_quality, as_of)
    snapshot_age = compute_snapshot_age(snapshot_timestamp, as_of) if snapshot_timestamp else None
    return OptionsSnapshotState(
        data_state=data_state,
        snapshot_source=snapshot_source,
        snapshot_timestamp=snapshot_timestamp,
        snapshot_age_minutes=snapshot_age.minutes if snapshot_age else None,
        snapshot_age_label=snapshot_age.label if snapshot_age else None,
    )


def get_latest_close_price(db: Session, ticker: str) -> Decimal | None:
    """Most recent real closing price on record for ``ticker``, regardless
    of how far in the past it is -- callers decide whether/how to label its
    staleness (e.g. against DataFreshness), this just never fabricates one."""
    row = (
        db.query(PriceBar)
        .filter(PriceBar.ticker == ticker)
        .order_by(PriceBar.trade_date.desc())
        .first()
    )
    return row.close if row else None


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
