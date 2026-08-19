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
from typing import Literal

from sqlalchemy.orm import Session

from analytics.data_state import compute_options_data_state, compute_snapshot_age
from analytics.market_session import EASTERN, previous_trading_session_date
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
from models.enums import (
    DataState,
    MarketDataQuality,
    OptionsSnapshotAnchor,
    OptionsSnapshotPurpose,
    OptionType,
)
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


def _is_priceable(quotes: list[OptionQuote]) -> bool:
    return any(q.bid is not None or q.ask is not None or q.last_price is not None for q in quotes)


def _rows_at_timestamp(db: Session, company_id: int, ts: datetime) -> list[OptionsSnapshot]:
    return (
        db.query(OptionsSnapshot)
        .filter(OptionsSnapshot.company_id == company_id, OptionsSnapshot.snapshot_timestamp == ts)
        .order_by(OptionsSnapshot.expiration_date, OptionsSnapshot.strike)
        .all()
    )


SnapshotTier = Literal["current_priceable", "previous_priceable", "contracts_only", "none"]

# Phase 14.11 Part 3: the hard gate strategy generation must pass through
# before running at all -- not just a label. ACTIONABLE_CURRENT and
# ACTIONABLE_PREVIOUS_SESSION are the only two statuses real strategy
# candidates may be generated from; CONTRACTS_ONLY, STALE_RESEARCH_ONLY,
# and UNAVAILABLE must never reach strategy generation (see
# api/routers/research.py and services/decision_engine.py, both of which
# check this before calling generate_strategy_candidates).
ActionabilityStatus = Literal[
    "actionable_current",
    "actionable_previous_session",
    "stale_research_only",
    "contracts_only",
    "unavailable",
]


def compute_actionability(
    tier: SnapshotTier, snapshot_timestamp: datetime | None, as_of: datetime
) -> ActionabilityStatus:
    """Turns a snapshot-selection tier into the hard actionability gate
    (Phase 14.11 Part 3/4, tightened in 14.12 after a live MU check found
    the gap below). "current_priceable" means "the single most recently
    *collected* snapshot is priceable" -- it makes no promise about actual
    recency, since select_pricing_snapshot only falls back to an older
    snapshot when the latest one is *unpriceable*. A company whose latest
    (and possibly only) snapshot happens to be priceable but days old --
    nothing has re-collected since -- must not be labeled
    "actionable_current": that would silently back a live recommendation
    with genuinely stale prices, no staleness warning at all. Both
    "current_priceable" and "previous_priceable" are therefore judged the
    same way: a snapshot dated after the most recently completed session
    is genuinely live/current; one dated exactly on that session is a real,
    still-usable "previous session" snapshot; anything older is stale and
    must not back an actionable recommendation."""
    if tier == "none":
        return "unavailable"
    if tier == "contracts_only":
        return "contracts_only"
    if snapshot_timestamp is None:
        return "stale_research_only"
    session_date = snapshot_timestamp.astimezone(EASTERN).date()
    most_recent_completed = previous_trading_session_date(as_of)
    if tier == "current_priceable" and session_date > most_recent_completed:
        return "actionable_current"
    if session_date == most_recent_completed:
        return "actionable_previous_session"
    return "stale_research_only"


@dataclass(frozen=True)
class PricingSnapshotSelection:
    """The result of the canonical snapshot-selection policy (Phase 14.10
    Part D) -- what strategy pricing and implied-move calculation should
    actually use, and honest provenance about why. Never fabricates a
    price: ``contracts_only`` still carries real contracts/Greeks, just no
    usable bid/ask/last on any of them.
    """

    quotes: list[OptionQuote]
    tier: SnapshotTier
    is_fallback: bool
    purpose: str | None
    snapshot_timestamp: datetime | None


def select_pricing_snapshot(db: Session, company: Company) -> PricingSnapshotSelection:
    """Canonical snapshot-selection policy: (1) the current (most recently
    collected) snapshot, if at least one contract has a real bid/ask/last;
    else (2) the most recent *previously* stored snapshot that is
    priceable -- explicitly labeled as a fallback, never silently
    presented as current, and never relabeled "previous close" unless it
    was actually captured with that purpose (see
    models.enums.OptionsSnapshotPurpose); else (3) the current snapshot's
    real contracts/Greeks with no pricing, rather than inventing one; else
    (4) nothing, when no snapshot has ever been collected.
    """
    latest_ts = _latest_snapshot_timestamp(db, company.id)
    if latest_ts is None:
        return PricingSnapshotSelection(
            quotes=[], tier="none", is_fallback=False, purpose=None, snapshot_timestamp=None
        )

    latest_rows = _rows_at_timestamp(db, company.id, latest_ts)
    latest_quotes = [_to_option_quote(r, company.ticker) for r in latest_rows]
    latest_purpose = (
        latest_rows[0].purpose.value if latest_rows and latest_rows[0].purpose else None
    )

    if _is_priceable(latest_quotes):
        return PricingSnapshotSelection(
            quotes=latest_quotes,
            tier="current_priceable",
            is_fallback=False,
            purpose=latest_purpose,
            snapshot_timestamp=latest_ts,
        )

    past_timestamps = (
        db.query(OptionsSnapshot.snapshot_timestamp)
        .filter(
            OptionsSnapshot.company_id == company.id,
            OptionsSnapshot.snapshot_timestamp < latest_ts,
        )
        .distinct()
        .order_by(OptionsSnapshot.snapshot_timestamp.desc())
        .all()
    )
    for (ts,) in past_timestamps:
        rows = _rows_at_timestamp(db, company.id, ts)
        quotes = [_to_option_quote(r, company.ticker) for r in rows]
        if _is_priceable(quotes):
            purpose = rows[0].purpose.value if rows and rows[0].purpose else None
            return PricingSnapshotSelection(
                quotes=quotes,
                tier="previous_priceable",
                is_fallback=True,
                purpose=purpose,
                snapshot_timestamp=ts,
            )

    return PricingSnapshotSelection(
        quotes=latest_quotes,
        tier="contracts_only",
        is_fallback=False,
        purpose=latest_purpose,
        snapshot_timestamp=latest_ts,
    )


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
        anchor=row.anchor.value if row.anchor else None,
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

    Uses the canonical snapshot-selection policy (select_pricing_snapshot,
    Phase 14.10 Part D): when the current snapshot has no priceable
    contract, falls back to the most recent stored snapshot that IS
    priceable rather than giving up -- this is what lets implied move stay
    computable through a frozen/closed-market current chain, as long as
    some real prior snapshot for this company has usable pricing.

    Returns None -- never a fabricated result -- when: no options quotes
    have been ingested for this company yet (in any snapshot), no matching
    expiration is present in the selected chain, no ATM call+put pair
    exists at the chosen expiration, or no price data exists to determine
    the underlying price as of the selected snapshot.
    """
    selection = select_pricing_snapshot(db, company)
    if not selection.quotes or selection.snapshot_timestamp is None:
        return None

    snapshot_timestamp = selection.snapshot_timestamp
    quotes = selection.quotes
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
    purpose: OptionsSnapshotPurpose = OptionsSnapshotPurpose.INTRADAY,
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
            purpose=purpose,
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


def capture_close_snapshot(
    db: Session,
    provider: OptionsDataProvider,
    company: Company,
    earnings_date: date | None,
    as_of: datetime,
) -> list[OptionsSnapshot] | None:
    """Deliberately captures a snapshot tagged ``OptionsSnapshotPurpose.CLOSE``
    (Phase 14.10 Part D4) -- the ONLY code path that ever sets this purpose,
    which is what lets the snapshot-selection fallback (select_pricing_snapshot)
    honestly say "Previous close" instead of the more conservative
    "Previous-session snapshot" for a fallback result. Meant to be run
    manually or via a user-owned cron/launchd job near a real market close,
    while the IBKR Gateway happens to be authenticated -- this project makes
    no always-on guarantee here (the Gateway requires local auth and the
    machine may be offline/asleep at close), so a missed close is simply a
    missed close, never silently invented later.

    Same same-day dedup as ``collect_options_snapshot_now`` -- running this
    twice near the same close doesn't create two competing "close" rows.
    """
    today = as_of.date()
    if _already_collected_today(db, company.id, today):
        return None
    return _fetch_and_persist_options_snapshot(
        db, provider, company, earnings_date, as_of, purpose=OptionsSnapshotPurpose.CLOSE
    )


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
class OptionsMarketState:
    """The one canonical "what's the options data right now" answer --
    Dashboard, Company Overview / Upcoming Earnings, Strategy Lab, and
    System Status all read this instead of each independently inferring
    availability from a different signal (that divergence is exactly what
    let AVGO show a real 22-contract chain in Strategy Lab while Upcoming
    Earnings claimed no options data existed at all -- Upcoming Earnings
    was gating on VolatilitySnapshot existing, which fails whenever no
    contract has a priceable bid/ask, even though the chain itself is
    real). ``reason`` is the single human-readable explanation every
    surface should show verbatim rather than writing its own.
    """

    chain_exists: bool
    contract_count: int
    priceable_contract_count: int
    has_bid_ask: bool
    has_iv: bool
    has_greeks: bool
    implied_move_available: bool
    earnings_anchored: bool | None
    expiration: date | None
    source: str | None
    snapshot_timestamp: datetime | None
    snapshot_age_minutes: int | None
    snapshot_age_label: str | None
    market_data_quality: str | None
    data_state: DataState
    reason: str
    # Snapshot-selection provenance (Phase 14.10 Part D) -- which tier of
    # the fallback policy actually produced ``raw_chain``. snapshot_tier
    # defaults to "current_priceable"/"none" for callers that don't pass a
    # PricingSnapshotSelection (kept backward compatible).
    snapshot_tier: SnapshotTier
    is_fallback_snapshot: bool
    snapshot_purpose: str | None
    # Phase 14.11 Part 3: the hard actionability gate. Only
    # "actionable_current"/"actionable_previous_session" may back real
    # strategy generation -- see compute_actionability's docstring.
    actionability: ActionabilityStatus


def compute_options_market_state(
    raw_chain: list[OptionQuote],
    as_of: datetime,
    volatility: VolatilitySnapshot | None,
    selection: "PricingSnapshotSelection | None" = None,
) -> OptionsMarketState:
    """Pure function over an already-fetched chain -- callers that already
    hold the chain (e.g. to also render its quotes) pass it in rather than
    triggering a second query. ``volatility`` is whatever
    ``get_latest_volatility_snapshot`` (or a fresh compute) returned -- the
    real, authoritative signal for whether an implied move could actually
    be calculated, since that depends on more than just "some contract has
    a bid/ask" (a matching ATM call/put pair and a real underlying price
    are also required; see compute_and_persist_volatility_snapshot).

    ``selection`` is the result of ``select_pricing_snapshot`` when the
    caller used the fallback-aware policy (Strategy Lab, Upcoming
    Earnings, AI Decision) -- omitted by callers still using the plain
    "always current" chain (e.g. the raw Option Chain viewer), which keeps
    this function's snapshot-selection provenance honestly reported as
    "current_priceable"/"none" rather than claiming a fallback happened
    when it didn't.
    """
    if not raw_chain:
        return OptionsMarketState(
            chain_exists=False,
            contract_count=0,
            priceable_contract_count=0,
            has_bid_ask=False,
            has_iv=False,
            has_greeks=False,
            implied_move_available=False,
            earnings_anchored=None,
            expiration=None,
            source=None,
            snapshot_timestamp=None,
            snapshot_age_minutes=None,
            snapshot_age_label=None,
            market_data_quality=None,
            data_state=DataState.NOT_COLLECTED,
            reason="No real options-chain snapshot has been collected yet.",
            snapshot_tier="none",
            is_fallback_snapshot=False,
            snapshot_purpose=None,
            actionability="unavailable",
        )

    snapshot_timestamp = raw_chain[0].snapshot_timestamp
    source = raw_chain[0].source_provider
    quality = raw_chain[0].market_data_quality
    earnings_anchored = raw_chain[0].anchor == "earnings_anchored" if raw_chain[0].anchor else None
    contract_count = len(raw_chain)
    priceable = [
        q for q in raw_chain if q.bid is not None or q.ask is not None or q.last_price is not None
    ]
    has_bid_ask = len(priceable) > 0
    has_iv = any(q.implied_volatility is not None for q in raw_chain)
    has_greeks = any(q.delta is not None for q in raw_chain)
    implied_move_available = volatility is not None
    snapshot_tier: SnapshotTier = (
        selection.tier if selection else ("current_priceable" if has_bid_ask else "contracts_only")
    )
    is_fallback_snapshot = selection.is_fallback if selection else False
    snapshot_purpose = selection.purpose if selection else None
    actionability = compute_actionability(snapshot_tier, snapshot_timestamp, as_of)
    expiration = (
        volatility.near_term_expiration
        if volatility is not None
        else min((q.expiration_date for q in raw_chain), default=None)
    )

    data_state = compute_options_data_state(snapshot_timestamp, quality, as_of)
    age = compute_snapshot_age(snapshot_timestamp, as_of)

    if is_fallback_snapshot:
        # A deliberate fallback to an older, priceable snapshot (Phase
        # 14.10 Part D) -- forced into PREVIOUS_SESSION labeling regardless
        # of what compute_options_data_state's own calendar-day check says,
        # since a snapshot this project chose to fall back to is never
        # "current" by definition. Only ever called "previous close" when
        # it was actually captured with that purpose (see
        # OptionsSnapshotPurpose) -- otherwise labeled by its real
        # timestamp, never guessed.
        data_state = DataState.PREVIOUS_SESSION
        if actionability == "stale_research_only":
            # Phase 14.11 Part 4: a fallback more than one real trading
            # session old is a HARD GATE, not merely an older label --
            # callers (Strategy Lab, AI Decision) must refuse to generate
            # actionable strategy candidates from it.
            reason = (
                "STALE -- RESEARCH ONLY. The current chain has no priceable quotes, and the "
                f"latest priceable chain on record is from {age.label} ago, more than one real "
                "US trading session back. A newer priceable option chain is required before "
                "recommendations can be generated."
            )
        else:
            label = (
                "Previous close" if snapshot_purpose == "close" else "Previous-session snapshot"
            )
            time_et = snapshot_timestamp.astimezone(EASTERN).strftime("%H:%M ET")
            reason = (
                f"{label}, {time_et} ({age.label} ago) used because the current chain has no "
                f"priceable quotes -- {contract_count} contracts, real pricing carried forward "
                "from this earlier, still-priceable snapshot rather than treated as unavailable."
            )
    elif data_state in (DataState.PREVIOUS_SESSION, DataState.STALE):
        # Not a deliberate fallback (the latest-collected snapshot IS this
        # one) -- but compute_options_data_state only distinguishes "same
        # calendar day" from "not," so a snapshot several real trading
        # sessions old and one exactly one session old both land here.
        # actionability (already computed above, same date-based check as
        # the fallback branch) is what actually tells them apart -- trust
        # it, not the coarser data_state, for both the label and the gate.
        if actionability == "stale_research_only":
            data_state = DataState.STALE
            reason = (
                "STALE -- RESEARCH ONLY. The latest options-chain snapshot on record "
                f"({contract_count} contracts) is from {age.label} ago, more than one real US "
                "trading session back, and nothing newer has been collected since. A newer "
                "priceable option chain is required before recommendations can be generated."
            )
        else:
            data_state = DataState.PREVIOUS_SESSION
            reason = (
                f"Real options-chain snapshot with {contract_count} contracts, but it's from the "
                f"previous trading session ({age.label} ago, nothing newer collected since) -- "
                "treat any pricing as indicative only, not live."
            )
    elif implied_move_available:
        reason = (
            f"Real options-chain snapshot with {contract_count} contracts; an implied move was "
            "computed from priceable quotes."
        )
    elif has_bid_ask:
        reason = (
            f"Real options-chain snapshot with {contract_count} contracts and usable bid/ask on "
            "at least one, but an implied move couldn't be computed -- no matching "
            "at-the-money call/put pair or no underlying price on record as of the snapshot."
        )
    elif has_iv or has_greeks:
        reason = (
            f"Real options-chain snapshot with {contract_count} contracts -- implied volatility "
            "and Greeks are available, but no contract has a usable bid/ask/last price yet, so "
            "an implied move can't be computed. Common before market open or on delayed quotes."
        )
    else:
        reason = (
            f"Real options-chain snapshot with {contract_count} contracts, but no pricing or "
            "volatility data is available on any contract yet."
        )

    if earnings_anchored is False:
        reason += " This snapshot is not anchored to a confirmed earnings date."

    return OptionsMarketState(
        chain_exists=True,
        contract_count=contract_count,
        priceable_contract_count=len(priceable),
        has_bid_ask=has_bid_ask,
        has_iv=has_iv,
        has_greeks=has_greeks,
        implied_move_available=implied_move_available,
        earnings_anchored=earnings_anchored,
        expiration=expiration,
        source=source,
        snapshot_timestamp=snapshot_timestamp,
        snapshot_age_minutes=age.minutes,
        snapshot_age_label=age.label,
        market_data_quality=quality,
        data_state=data_state,
        reason=reason,
        snapshot_tier=snapshot_tier,
        is_fallback_snapshot=is_fallback_snapshot,
        snapshot_purpose=snapshot_purpose,
        actionability=actionability,
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
