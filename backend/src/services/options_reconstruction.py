"""Reconstructs a completed US regular trading session's option-market
close state from IBKR historical data (Phase 14.13) -- the mandatory
correction to the previous architecture, which used "whatever the most
recently stored snapshot happens to be" while the market is closed. That
is wrong: a 13:00 ET intraday snapshot is not the close, even if it's the
only thing on record.

Core rule:
  MARKET OPEN            -> a fresh current priceable snapshot.
  CLOSED/PRE-MARKET/etc.  -> the most recent COMPLETED regular session's
                             CLOSE market state -- reconstructed from real
                             IBKR historical bars when nothing adequate
                             was captured live.

Confirmed live against a real authenticated Gateway (WMT, 2026-08-19):
IBKR's Client Portal ``/iserver/marketdata/history`` endpoint does return
real historical bars for OPTION conids on this account, at 1-minute
granularity, trade/last-price only (no bid/ask bar type is exposed by
this REST API, unlike the classic TWS API) -- see
providers/ibkr_historical.py's module docstring for the full technical
record. Every reconstructed contract is therefore tagged
``pricing_source="historical_last"``, never silently promoted to a
fabricated bid/ask.

The canonical entrypoint is ``resolve_best_actionable_option_market`` --
every caller that needs "the best real option market state for this
company right now" should use this instead of calling
``select_pricing_snapshot`` directly, since that older function only ever
looks at what's already stored (see its own docstring) and has no
awareness of session-close reconstruction at all.
"""

from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Literal

from sqlalchemy import and_
from sqlalchemy.orm import Session

from analytics.market_session import (
    EASTERN,
    MarketSession,
    get_market_session,
    previous_trading_session_date,
)
from analytics.options.black_scholes import price_and_greeks
from core.config import Settings, get_settings
from models.company import Company
from models.enums import GreeksSource, OptionType
from models.options_snapshot import OptionsSnapshot
from providers.base import OptionsDataProvider
from providers.factory import get_options_provider
from providers.ibkr_client import IBKRError
from providers.ibkr_historical import last_bar_at_or_before
from providers.types import OptionQuote
from services.options_analytics import (
    PricingSnapshotSelection,
    _fetch_and_persist_options_snapshot,  # noqa: PLC2701 -- shared private helper, see below
    _to_option_quote,  # noqa: PLC2701 -- shared private helper, see strategy_generation.py's identical reuse
    select_pricing_snapshot,
)
from services.provider_settings import get_app_provider_settings

# The close-reconstruction target window (Phase 14.13 mandate section 2):
# preferred close data is the last real observation between these two ET
# times. Documented rather than settings-driven for now, matching this
# project's existing convention for similar bounded-window constants (see
# providers/ibkr_options.py's STRIKES_AROUND_ATM) -- a future pass could
# move this to core/config.py if a real need to tune it per-deployment
# arises.
CLOSE_WINDOW_START = time(15, 55)
CLOSE_WINDOW_END = time(16, 0)

# Maximum acceptable skew (Phase 14.13 Part 10) between the reconstructed
# underlying's own observation time and an option contract's observation
# time before that contract is excluded from the reconstructed snapshot
# entirely -- rebuilding a chain from bars that don't actually represent
# the same instant would silently produce an incoherent implied move.
# Conservative and documented rather than assumed: real market bars in the
# close window are 1-minute apart, so 5 minutes comfortably covers normal
# per-conid bar-availability jitter without accepting genuinely stale data.
MAX_UNDERLYING_OPTION_SKEW = timedelta(minutes=5)

# How many strikes on each side of the underlying's close price to attempt
# to reconstruct -- bounded per Phase 14.13 Part 14 ("reconstruct only
# what the engine needs"), sized to cover every strategy type this
# project's candidate engine builds (spreads/condors/butterflies need a
# few strikes beyond the pure ATM straddle).
RECONSTRUCTION_STRIKES_AROUND_ATM = 6

ChainQuality = Literal["good", "acceptable", "poor", "untradeable"]

_PURPOSE_RANK: dict[str, int] = {
    "CLOSE": 0,
    "RECONSTRUCTED_CLOSE": 1,
    "NEAR_CLOSE": 1,
    "INTRADAY": 2,
    "EARNINGS": 2,
    "MANUAL": 2,
}
_QUALITY_RANK: dict[ChainQuality, int] = {"good": 0, "acceptable": 1, "poor": 2, "untradeable": 3}


@dataclass(frozen=True)
class ChainQualitySummary:
    contract_count: int
    priceable_contract_count: int
    bid_ask_coverage: float
    last_coverage: float
    iv_coverage: float
    greeks_coverage: float
    oi_coverage: float
    volume_coverage: float
    median_spread_pct: Decimal | None
    quality: ChainQuality


def classify_chain_quality(quotes: list[OptionQuote]) -> ChainQualitySummary:
    """Deterministic, documented thresholds (Phase 14.13 Part 16): quality
    is driven purely by the fraction of contracts with a usable price
    (bid/ask or last) -- >=80% priceable is GOOD, >=40% is ACCEPTABLE,
    any priceable contract at all below that is POOR, and zero priceable
    contracts is UNTRADEABLE. Only GOOD/ACCEPTABLE should normally back an
    actionable recommendation (enforced by callers, not this function)."""
    n = len(quotes)
    if n == 0:
        return ChainQualitySummary(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, None, "untradeable")

    priceable = [
        q for q in quotes if q.bid is not None or q.ask is not None or q.last_price is not None
    ]
    bid_ask = [q for q in quotes if q.bid is not None and q.ask is not None]
    spreads = [
        (q.ask - q.bid) / ((q.bid + q.ask) / 2)
        for q in bid_ask
        if q.bid is not None and q.ask is not None and q.bid > 0 and q.ask > 0
    ]
    median_spread = None
    if spreads:
        ordered = sorted(spreads)
        mid = len(ordered) // 2
        median_spread = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2

    priceable_ratio = len(priceable) / n
    if priceable_ratio <= 0:
        quality: ChainQuality = "untradeable"
    elif priceable_ratio >= 0.8:
        quality = "good"
    elif priceable_ratio >= 0.4:
        quality = "acceptable"
    else:
        quality = "poor"

    return ChainQualitySummary(
        contract_count=n,
        priceable_contract_count=len(priceable),
        bid_ask_coverage=len(bid_ask) / n,
        last_coverage=sum(1 for q in quotes if q.last_price is not None) / n,
        iv_coverage=sum(1 for q in quotes if q.implied_volatility is not None) / n,
        greeks_coverage=sum(1 for q in quotes if q.delta is not None) / n,
        oi_coverage=sum(1 for q in quotes if q.open_interest is not None) / n,
        volume_coverage=sum(1 for q in quotes if q.volume is not None) / n,
        median_spread_pct=median_spread,
        quality=quality,
    )


def determine_target_close_window(as_of: datetime) -> tuple[date, datetime, datetime]:
    """The most recently COMPLETED US regular trading session, and its
    real close-reconstruction window (Phase 14.13 Part 1/2) in UTC --
    never inferred from "the latest row in the database"."""
    target_date = previous_trading_session_date(as_of)
    window_start_et = datetime.combine(target_date, CLOSE_WINDOW_START, tzinfo=EASTERN)
    window_end_et = datetime.combine(target_date, CLOSE_WINDOW_END, tzinfo=EASTERN)
    return target_date, window_start_et.astimezone(UTC), window_end_et.astimezone(UTC)


@dataclass(frozen=True)
class PersistedCloseCandidate:
    quotes: list[OptionQuote]
    purpose: str
    snapshot_timestamp: datetime
    quality: ChainQualitySummary


def find_best_persisted_close_snapshot(
    db: Session, company: Company, target_session_date: date
) -> PersistedCloseCandidate | None:
    """Every distinct snapshot already stored for ``target_session_date``
    (its real US-Eastern calendar date, however the row's own UTC
    timestamp reads), ranked by purpose (close > near_close/
    reconstructed_close > intraday), then distance from 16:00 ET, then
    quality -- never just "the latest row" (Phase 14.13 Part 3). A 15:58
    snapshot beats a 13:00 one even though 13:00 might be a "more recent"
    calendar timestamp on some other, earlier session.
    """
    day_start_et = datetime.combine(target_session_date, time(0, 0), tzinfo=EASTERN)
    day_start_utc = day_start_et.astimezone(UTC)
    day_end_utc = datetime.combine(
        target_session_date + timedelta(days=1), time(0, 0), tzinfo=EASTERN
    ).astimezone(UTC)

    rows = (
        db.query(OptionsSnapshot)
        .filter(
            and_(
                OptionsSnapshot.company_id == company.id,
                OptionsSnapshot.snapshot_timestamp >= day_start_utc,
                OptionsSnapshot.snapshot_timestamp < day_end_utc,
            )
        )
        .all()
    )
    if not rows:
        return None

    groups: dict[tuple[datetime, str], list[OptionsSnapshot]] = defaultdict(list)
    for row in rows:
        if row.snapshot_timestamp.astimezone(EASTERN).date() != target_session_date:
            continue
        purpose = row.purpose.value if row.purpose else "intraday"
        groups[(row.snapshot_timestamp, purpose)].append(row)
    if not groups:
        return None

    close_target_et = datetime.combine(target_session_date, CLOSE_WINDOW_END, tzinfo=EASTERN)

    def sort_key(
        item: tuple[tuple[datetime, str], list[OptionsSnapshot]],
    ) -> tuple[int, float, int]:
        (ts, purpose), group_rows = item
        distance = abs((close_target_et - ts.astimezone(EASTERN)).total_seconds())
        quotes = [_to_option_quote(r, company.ticker) for r in group_rows]
        quality = classify_chain_quality(quotes)
        return (
            _PURPOSE_RANK.get(purpose.upper(), 9),
            distance,
            _QUALITY_RANK[quality.quality],
        )

    (best_ts, best_purpose), best_rows = min(groups.items(), key=sort_key)
    quotes = [_to_option_quote(r, company.ticker) for r in best_rows]
    return PersistedCloseCandidate(
        quotes=quotes,
        purpose=best_purpose,
        snapshot_timestamp=best_ts,
        quality=classify_chain_quality(quotes),
    )


@dataclass(frozen=True)
class ReconstructionResult:
    succeeded: bool
    reason: str
    quotes: list[OptionQuote]
    quality: ChainQualitySummary | None
    underlying_price: Decimal | None
    underlying_timestamp: datetime | None
    expiration: date | None
    snapshot_timestamp: datetime | None


def _risk_free_rate_assumption() -> Decimal:
    """A single, documented, deterministic assumption (Phase 14.13 Part
    15) -- never fitted or fetched per-request. Matches the same 0%
    default already used by analytics/options/black_scholes.py's own
    dividend_yield default for the equivalent "no better real signal"
    case; a short-dated rate assumption barely moves near-term option
    Greeks anyway."""
    return Decimal("0.04")


def _implied_vol_from_price(
    option_type: OptionType,
    market_price: Decimal,
    spot: Decimal,
    strike: Decimal,
    time_to_expiry_years: Decimal,
    rate: Decimal,
) -> Decimal | None:
    """Bisection search for the volatility that reproduces
    ``market_price`` under Black-Scholes -- this project has no existing
    IV solver (only forward price_and_greeks), so this is new, small, pure
    math, deterministic and side-effect-free. Returns None rather than a
    guess when the search doesn't converge to a sane result (e.g. the
    price is outside any achievable Black-Scholes value for this
    spot/strike/TTE, which does happen for deep ITM/OTM reconstructed
    trade prices)."""
    if market_price <= 0 or spot <= 0 or strike <= 0 or time_to_expiry_years <= 0:
        return None
    lo, hi = 0.001, 5.0
    target = float(market_price)
    for _ in range(60):
        mid = (lo + hi) / 2
        try:
            greeks = price_and_greeks(
                option_type,
                float(spot),
                float(strike),
                float(time_to_expiry_years),
                float(rate),
                mid,
            )
        except ValueError:
            return None
        if greeks.price > target:
            hi = mid
        else:
            lo = mid
        if hi - lo < 1e-5:
            break
    result = Decimal(str((lo + hi) / 2))
    if result <= Decimal("0.001") or result >= Decimal("4.99"):
        return None
    return result


def recompute_deterministic_greeks(
    option_type: OptionType,
    market_price: Decimal,
    spot: Decimal,
    strike: Decimal,
    expiration: date,
    as_of: datetime,
) -> tuple[Decimal, Decimal, Decimal, Decimal, Decimal] | None:
    """IV/delta/gamma/theta/vega recomputed from a reconstructed historical
    trade price via Black-Scholes (Phase 14.13 Part 15) -- used only when
    IBKR's historical bars carry no Greeks of their own (confirmed live:
    they don't). Never mixes today's live IBKR Greeks with yesterday's
    reconstructed premium. Returns None when the price can't be
    rationalized to a sane implied vol (see _implied_vol_from_price)."""
    tte_days = (expiration - as_of.date()).days
    tte_years = Decimal(max(tte_days, 1)) / Decimal(365)
    rate = _risk_free_rate_assumption()
    iv = _implied_vol_from_price(option_type, market_price, spot, strike, tte_years, rate)
    if iv is None:
        return None
    greeks = price_and_greeks(
        option_type, float(spot), float(strike), float(tte_years), float(rate), float(iv)
    )
    return (
        iv,
        Decimal(str(greeks.delta)),
        Decimal(str(greeks.gamma)),
        Decimal(str(greeks.theta)),
        Decimal(str(greeks.vega)),
    )


def reconstruct_close_snapshot(
    db: Session,
    company: Company,
    provider: OptionsDataProvider,
    target_session_date: date,
    earnings_date: date | None,
    source_provider_tag: str = "ibkr_web",
) -> ReconstructionResult:
    """Rebuilds ``target_session_date``'s option-market close state from
    real IBKR historical bars (Phase 14.13 Part 4-11). Never invents a
    contract, a bid/ask, or a Greek -- every value traces back either to a
    real historical bar or a deterministic recomputation clearly tagged as
    such (GreeksSource.BLACK_SCHOLES).

    ``provider`` is any ``OptionsDataProvider`` that implements the three
    reconstruction methods declared on the ABC (see providers/base.py) --
    IBKR TWS Migration Phase 3 readiness: this used to require a concrete
    ``IBKROptionsProvider`` plus a separate ``IBKRClient`` for historical
    bars specifically, hard-coupling reconstruction to the Web transport
    even when the app's configured IBKR transport was TWS. Both providers/
    ibkr_options.py and providers/ibkr_tws_options.py now implement all
    three methods identically in shape (real conids/strikes/bars, honest
    ``None``/empty on failure) -- this function itself never changed how
    it interprets or scores what comes back.

    ``source_provider_tag`` (Section 8, source provenance) -- honestly
    records which real transport actually produced these reconstructed
    quotes ("ibkr_web" or "ibkr_tws", matching the same values providers/
    ibkr_options.py and providers/ibkr_tws_options.py write on their own
    live-collected quotes) on every ``OptionQuote`` this function builds.
    Deliberately a caller-supplied string rather than something this
    function derives itself via an isinstance check on ``provider`` --
    the caller (``_resolve_via_previous_session_close`` below) already
    knows which transport it resolved from ``settings.ibkr_provider``,
    and this keeps ``provider`` genuinely provider-neutral in type here.
    Defaults to "ibkr_web" (this function's only real caller today) so a
    test or script that constructs a Web provider directly and omits this
    argument keeps getting the pre-Section-8 tag.
    """
    window_end_et = datetime.combine(target_session_date, CLOSE_WINDOW_END, tzinfo=EASTERN)
    window_end_utc = window_end_et.astimezone(UTC)

    try:
        expiration = provider.resolve_expiration_for_reconstruction(
            company.ticker, target_session_date, earnings_date
        )
    except IBKRError as exc:
        return ReconstructionResult(
            succeeded=False,
            reason=f"IBKR historical reconstruction failed while resolving an expiration: {exc}",
            quotes=[],
            quality=None,
            underlying_price=None,
            underlying_timestamp=None,
            expiration=None,
            snapshot_timestamp=None,
        )
    if expiration is None:
        return ReconstructionResult(
            succeeded=False,
            reason="IBKR historical reconstruction failed: no real listed expiration could be "
            "resolved for this company.",
            quotes=[],
            quality=None,
            underlying_price=None,
            underlying_timestamp=None,
            expiration=None,
            snapshot_timestamp=None,
        )

    try:
        underlying_conid, _live_price_hint, contracts = provider.discover_contracts_for_expiration(
            company.ticker, expiration
        )
    except IBKRError as exc:
        return ReconstructionResult(
            succeeded=False,
            reason=f"IBKR historical reconstruction failed while discovering contracts: {exc}",
            quotes=[],
            quality=None,
            underlying_price=None,
            underlying_timestamp=None,
            expiration=expiration,
            snapshot_timestamp=None,
        )

    # Underlying reconstruction first (Part 9) -- every option contract's
    # premium is only kept if it falls within MAX_UNDERLYING_OPTION_SKEW of
    # this same observation (Part 10), so option reconstruction below
    # depends on this having succeeded.
    try:
        underlying_bars = provider.get_historical_bars(
            underlying_conid, bar="1min", period="1d", end_time=window_end_utc
        )
    except IBKRError as exc:
        return ReconstructionResult(
            succeeded=False,
            reason=f"IBKR historical reconstruction failed while fetching the underlying: {exc}",
            quotes=[],
            quality=None,
            underlying_price=None,
            underlying_timestamp=None,
            expiration=expiration,
            snapshot_timestamp=None,
        )
    underlying_bar = last_bar_at_or_before(underlying_bars, window_end_utc)
    if underlying_bar is None:
        return ReconstructionResult(
            succeeded=False,
            reason=f"IBKR historical reconstruction failed: no underlying bars were returned for "
            f"{target_session_date}.",
            quotes=[],
            quality=None,
            underlying_price=None,
            underlying_timestamp=None,
            expiration=expiration,
            snapshot_timestamp=None,
        )
    underlying_price = underlying_bar.close
    underlying_timestamp = underlying_bar.timestamp

    reconstructed_at = datetime.now(UTC)
    quotes: list[OptionQuote] = []
    skipped_skew = 0
    skipped_no_data = 0
    for strike, right, option_conid in contracts:
        try:
            bars = provider.get_historical_bars(
                option_conid, bar="1min", period="1d", end_time=window_end_utc
            )
        except IBKRError:
            skipped_no_data += 1
            continue
        bar = last_bar_at_or_before(bars, window_end_utc)
        if bar is None:
            skipped_no_data += 1
            continue
        if abs(bar.timestamp - underlying_timestamp) > MAX_UNDERLYING_OPTION_SKEW:
            skipped_skew += 1
            continue

        option_type = OptionType.CALL if right == "C" else OptionType.PUT
        greeks = recompute_deterministic_greeks(
            option_type, bar.close, underlying_price, strike, expiration, bar.timestamp
        )
        quotes.append(
            OptionQuote(
                ticker=company.ticker,
                snapshot_timestamp=bar.timestamp,
                expiration_date=expiration,
                strike=strike,
                option_type=option_type.value,
                last_price=bar.close,
                volume=int(bar.volume) if bar.volume is not None else None,
                implied_volatility=greeks[0] if greeks else None,
                delta=greeks[1] if greeks else None,
                gamma=greeks[2] if greeks else None,
                theta=greeks[3] if greeks else None,
                vega=greeks[4] if greeks else None,
                market_data_quality="frozen",
                external_contract_id=str(option_conid),
                source_provider=source_provider_tag,
                retrieved_at=reconstructed_at,
                anchor="earnings_anchored" if earnings_date is not None else "general_current",
                underlying_price=underlying_price,
                underlying_timestamp=underlying_timestamp,
            )
        )

    if not quotes:
        reasons = []
        if skipped_no_data:
            reasons.append(f"{skipped_no_data} contract(s) returned no historical bars")
        if skipped_skew:
            reasons.append(
                f"{skipped_skew} contract(s) exceeded the underlying time-skew threshold"
            )
        detail = "; ".join(reasons) if reasons else "no contracts were discovered"
        return ReconstructionResult(
            succeeded=False,
            reason=f"IBKR historical reconstruction found no usable contract pricing ({detail}).",
            quotes=[],
            quality=None,
            underlying_price=underlying_price,
            underlying_timestamp=underlying_timestamp,
            expiration=expiration,
            snapshot_timestamp=None,
        )

    quality = classify_chain_quality(quotes)
    snapshot_ts = max(q.snapshot_timestamp for q in quotes)
    return ReconstructionResult(
        succeeded=True,
        reason=(
            f"Reconstructed {len(quotes)}/{len(contracts)} contract(s) from IBKR historical data "
            f"({skipped_no_data} had no historical bars, {skipped_skew} exceeded the underlying "
            "time-skew threshold)."
        ),
        quotes=quotes,
        quality=quality,
        underlying_price=underlying_price,
        underlying_timestamp=underlying_timestamp,
        expiration=expiration,
        snapshot_timestamp=snapshot_ts,
    )


def persist_reconstruction(
    db: Session, company: Company, result: ReconstructionResult, target_session_date: date
) -> list[OptionsSnapshot]:
    """Persists a successful reconstruction as real OptionsSnapshot rows
    (Phase 14.13 Part 11/12) so it's reused on future page loads instead
    of re-burning IBKR requests every refresh -- the DB itself is the
    cache; find_best_persisted_close_snapshot will find these rows next
    time via their purpose=RECONSTRUCTED_CLOSE tag. reconstructed_at
    (retrieved_at) is never conflated with market_timestamp
    (snapshot_timestamp): the former is "when we ran this," the latter is
    "what instant this data represents."
    """
    if not result.succeeded or not result.quotes:
        return []
    rows: list[OptionsSnapshot] = []
    for q in result.quotes:
        rows.append(
            OptionsSnapshot(
                company_id=company.id,
                snapshot_timestamp=q.snapshot_timestamp,
                expiration_date=q.expiration_date,
                strike=q.strike,
                option_type=OptionType(q.option_type),
                last_price=q.last_price,
                volume=q.volume,
                implied_volatility=q.implied_volatility,
                delta=q.delta,
                gamma=q.gamma,
                theta=q.theta,
                vega=q.vega,
                greeks_source=(
                    GreeksSource.BLACK_SCHOLES if q.implied_volatility is not None else None
                ),
                market_data_quality=None,
                external_contract_id=q.external_contract_id,
                # Section 8 -- inherits whatever tag reconstruct_close_
                # snapshot already stamped on this quote ("ibkr_web" or
                # "ibkr_tws"), rather than re-hardcoding a literal here
                # that could silently disagree with it.
                source_provider=q.source_provider,
                retrieved_at=q.retrieved_at,
                anchor=q.anchor or "general_current",
                purpose="RECONSTRUCTED_CLOSE",
                pricing_source="historical_last",
                reconstruction_source="ibkr_historical",
                underlying_price=result.underlying_price,
                underlying_timestamp=result.underlying_timestamp,
            )
        )
    db.add_all(rows)
    db.flush()
    return rows


@dataclass(frozen=True)
class MarketResolution:
    selection: PricingSnapshotSelection
    reconstruction_attempted: bool
    reconstruction_result: ReconstructionResult | None
    target_session_date: date | None


def _is_good_or_acceptable(quotes: list[OptionQuote]) -> bool:
    if not quotes:
        return False
    return classify_chain_quality(quotes).quality in ("good", "acceptable")


def _effective_options_provider(db: Session, settings: Settings) -> str:
    """Checks the owner's DB-stored override (Settings -> Data Providers)
    first, not just the raw env default -- an override to "ibkr" there is
    exactly what System Status already shows as the active provider."""
    app_settings = get_app_provider_settings(db)
    return (app_settings.options_primary or settings.options_provider).lower()


def _resolve_via_previous_session_close(
    db: Session,
    company: Company,
    as_of: datetime,
    earnings_date: date | None,
    settings: Settings,
    effective_provider: str,
) -> MarketResolution:
    """Shared fallback path (Phase 14.14): the immediately previous
    completed trading session's close, from whatever source actually has
    it -- persisted close/near-close/reconstructed_close first, else a
    real IBKR historical reconstruction. Used both when the market is
    closed (there's no "current" to speak of) and when the market is open
    but the current snapshot isn't priceable even after a live retry
    (Part CASE 2's fallback step). Never a random older intraday snapshot
    "just because it exists" -- if this fails too, the caller defers to
    select_pricing_snapshot's own actionability gate, which is honest
    about anything older (stale_research_only, hard-gated from strategy
    generation).
    """
    target_session_date, _window_start, _window_end = determine_target_close_window(as_of)

    persisted = find_best_persisted_close_snapshot(db, company, target_session_date)
    if persisted is not None and persisted.quality.quality in ("good", "acceptable"):
        return MarketResolution(
            selection=PricingSnapshotSelection(
                quotes=persisted.quotes,
                tier="previous_priceable",
                is_fallback=True,
                purpose=persisted.purpose.lower(),
                snapshot_timestamp=persisted.snapshot_timestamp,
            ),
            reconstruction_attempted=False,
            reconstruction_result=None,
            target_session_date=target_session_date,
        )

    if effective_provider != "ibkr":
        return MarketResolution(
            selection=select_pricing_snapshot(db, company),
            reconstruction_attempted=False,
            reconstruction_result=None,
            target_session_date=target_session_date,
        )

    # IBKR TWS Migration Phase 3 readiness -- routes through providers/
    # factory.py rather than constructing a concrete IBKROptionsProvider
    # directly, so this resolves to whichever real transport (Web or TWS)
    # settings.ibkr_provider currently selects. The former standalone
    # IBKRClient().ensure_authenticated() pre-check is gone: every
    # OptionsDataProvider method reconstruct_close_snapshot calls already
    # performs its own real readiness check first (self._client.ensure_
    # authenticated() on the Web adapter, self._connection.ensure_
    # connected() on the TWS adapter) and reports an honest, typed
    # IBKRError-derived ReconstructionResult.reason on failure -- strictly
    # more informative than the old short-circuit, which discarded the
    # real failure reason entirely.
    provider = get_options_provider(settings, override="ibkr", db=db)
    source_provider_tag = "ibkr_tws" if settings.ibkr_provider.lower() == "tws" else "ibkr_web"
    result = reconstruct_close_snapshot(
        db, company, provider, target_session_date, earnings_date, source_provider_tag
    )
    if (
        result.succeeded
        and result.quality is not None
        and result.quality.quality
        in (
            "good",
            "acceptable",
        )
    ):
        persist_reconstruction(db, company, result, target_session_date)
        return MarketResolution(
            selection=PricingSnapshotSelection(
                quotes=result.quotes,
                tier="previous_priceable",
                is_fallback=True,
                purpose="reconstructed_close",
                snapshot_timestamp=result.snapshot_timestamp,
            ),
            reconstruction_attempted=True,
            reconstruction_result=result,
            target_session_date=target_session_date,
        )

    # Reconstruction failed or produced poor/untradeable quality -- persist
    # nothing, and defer to select_pricing_snapshot's existing, correct
    # fallback/staleness-gate logic: an intraday snapshot may still
    # surface as research-only; a snapshot two-or-more sessions old is
    # still hard-gated as stale. This is CASE 4 ("neither current nor
    # previous-close data is usable") whenever that gate also comes up
    # empty/stale.
    return MarketResolution(
        selection=select_pricing_snapshot(db, company),
        reconstruction_attempted=True,
        reconstruction_result=result,
        target_session_date=target_session_date,
    )


def resolve_best_actionable_option_market(
    db: Session,
    company: Company,
    as_of: datetime,
    earnings_date: date | None,
    settings: Settings | None = None,
    *,
    force_live_refresh: bool = False,
) -> MarketResolution:
    """The single canonical resolver (Phase 14.13/14.14) every caller that
    needs "the best real option market state for this company right now"
    should use in place of calling select_pricing_snapshot directly.

    Driven by PRICEABILITY, never merely by whether the market is open
    (Phase 14.14 -- the real AVGO bug this fixes: market open plus a
    22-contract, zero-priceable "current" snapshot used to be accepted
    as-is with no fallback attempted at all, just because the market
    happened to be open).

      CASE 1: market open, current snapshot already GOOD/ACCEPTABLE and
        priceable, and the caller hasn't demanded a fresh read -> use it
        directly, no retry, no reconstruction.
      CASE 2: market open, current snapshot CONTRACTS_ONLY/UNTRADEABLE (or
        no chain at all) -- OR force_live_refresh=True and CASE 1 would
        otherwise have fired -> retry a real live IBKR fetch once. If that
        still isn't priceable -> fall through to the immediately-previous
        completed session's close (persisted or reconstructed), used as
        an explicitly labeled fallback (is_fallback=True,
        tier="previous_priceable") -- NEVER presented as real-time.
      CASE 3: market closed/pre-market/weekend/holiday -> the same
        previous-session-close fallback directly (no live retry -- there's
        no live quote to retry while the market is shut).
      CASE 4: neither current nor a usable previous close exists -> defer
        to select_pricing_snapshot's own actionability gate, which
        reports NO ACTIONABLE OPTIONS RECOMMENDATION (contracts_only /
        stale_research_only) rather than fabricating anything.

    ``force_live_refresh`` (V4.1 source-coherence fix, 2026-08-31) --
    real, confirmed root cause of an 8.6% decision-vs-entry underlying
    price gap on a real settled trade (DY, 2026-08-25): CASE 1's own
    "current_priceable" check is purely about PRICEABILITY, never about
    how long ago the snapshot was actually collected (see
    compute_actionability's own docstring in services/options_analytics.py,
    which already documents this exact gap for its own, coarser,
    date-level check) -- a same-session-day snapshot collected hours
    earlier by an unrelated periodic collection job passes it exactly the
    same as one collected seconds ago. services/benchmark_entry_capture.py
    always re-fetches live moments later regardless, so decision
    generation's own strike/target selection can silently anchor to a
    materially stale intraday price while the position it enters does not.
    Deliberately NOT a new numeric minutes-old threshold (no such
    established rule exists anywhere in this codebase -- see
    analytics/data_state.py's own day-and-session-boundary-only
    granularity): real, official decision generation
    (services/decision_engine.py's AUTO path, the only caller that
    actually commits a trade) instead simply always prefers a fresh live
    read when the market is open, by skipping straight to CASE 2's
    already-existing, already-tested live-retry-then-previous-session-
    fallback logic. Defaults to False so every other real caller (Strategy
    Lab, Upcoming Earnings, and any other read-only research view in
    api/routers/research.py) keeps its exact current "avoid an
    unnecessary live call" behavior, unchanged.
    """
    settings = settings or get_settings()
    market = get_market_session(as_of)

    current_selection = select_pricing_snapshot(db, company)
    current_is_good = current_selection.tier == "current_priceable" and _is_good_or_acceptable(
        current_selection.quotes
    )

    if market.session == MarketSession.REGULAR and current_is_good and not force_live_refresh:
        return MarketResolution(
            selection=current_selection,
            reconstruction_attempted=False,
            reconstruction_result=None,
            target_session_date=None,
        )

    effective_provider = _effective_options_provider(db, settings)

    if market.session == MarketSession.REGULAR:
        # CASE 2: market open but current isn't good enough -- a single
        # real live retry before falling back to the previous close. The
        # stored "current" snapshot may simply be stale from earlier in
        # the session (e.g. captured before quotes settled) -- worth one
        # fresh attempt, never a loop.
        if effective_provider == "ibkr":
            # IBKR TWS Migration Phase 3 readiness -- see the identical
            # factory-resolution comment in _resolve_via_previous_session_
            # close above; _fetch_and_persist_options_snapshot's own
            # get_option_chain call already performs the real readiness
            # check for whichever transport this resolves to, so no
            # separate pre-check is needed here either.
            try:
                provider = get_options_provider(settings, override="ibkr", db=db)
                _fetch_and_persist_options_snapshot(db, provider, company, earnings_date, as_of)
                retried_selection = select_pricing_snapshot(db, company)
                if retried_selection.tier == "current_priceable" and _is_good_or_acceptable(
                    retried_selection.quotes
                ):
                    return MarketResolution(
                        selection=retried_selection,
                        reconstruction_attempted=False,
                        reconstruction_result=None,
                        target_session_date=None,
                    )
            except IBKRError:
                pass  # fall through to the previous-session-close fallback below

    return _resolve_via_previous_session_close(
        db, company, as_of, earnings_date, settings, effective_provider
    )
