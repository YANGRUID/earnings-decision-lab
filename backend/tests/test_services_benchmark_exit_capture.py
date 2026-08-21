"""Phase 4.5 -- unit tests for services/benchmark_exit_capture.py. Uses
an in-memory fake OptionsDataProvider, never a live provider call.
Mirrors test_services_benchmark_entry_capture.py's structure exactly."""

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import ProgrammingError

from analytics.earnings_timing import compute_entry_exit_schedule
from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.entry_snapshot import EntrySnapshot
from models.enums import (
    AnnouncementTime,
    CaptureStatus,
    DecisionDirection,
    DecisionSnapshotStatus,
    EarningsTiming,
    OptionAction,
    OptionType,
    RiskProfile,
)
from models.exit_snapshot import ExitSnapshot
from models.settlement_capture_attempt import SettlementCaptureAttempt
from providers.base import OptionsDataProvider
from providers.ibkr_client import IBKRGatewayUnavailableError
from providers.types import KnownContract, OptionQuote, UnderlyingQuote
from services.benchmark_exit_capture import (
    EXIT_EARLY_CAPTURE_TOLERANCE,
    capture_benchmark_exit,
)
from services.decision_pipeline import LATE_CUTOFF_GRACE

EARNINGS_DATE = date(2026, 9, 16)  # a Wednesday -- real trading day either side
EXP = date(2026, 9, 18)


def _exit_schedule(
    earnings_date: date = EARNINGS_DATE,
    timing: AnnouncementTime = AnnouncementTime.AFTER_MARKET,
):
    return compute_entry_exit_schedule(earnings_date, timing)


def _due_now() -> datetime:
    return _exit_schedule().exit_timestamp


_UNSET = object()


class _FakeExitOptionsProvider(OptionsDataProvider):
    """``underlying`` defaults to a coherent live quote (price 100, same
    timestamp as ``_due_now()``) unless a test explicitly overrides it."""

    def __init__(
        self,
        quotes: list[OptionQuote] | None = None,
        raise_exc: Exception | None = None,
        underlying: UnderlyingQuote | None = _UNSET,  # type: ignore[assignment]
    ):
        self._quotes = quotes if quotes is not None else []
        self._raise_exc = raise_exc
        self._underlying = underlying

    def get_option_chain(
        self, ticker, as_of, expiration=None, reference_date=None, earnings_anchored=True
    ) -> list[OptionQuote]:
        return []

    def get_quotes_for_known_contracts(
        self, ticker: str, contracts: list[KnownContract], expiration: date, as_of: datetime
    ) -> list[OptionQuote]:
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._quotes

    def get_underlying_quote(self, ticker) -> UnderlyingQuote | None:
        if self._underlying is _UNSET:
            return _underlying()
        return self._underlying


def _underlying(
    price: Decimal | None = Decimal("100.00"),
    *,
    ts: datetime | None = None,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
) -> UnderlyingQuote:
    ts = ts or _due_now()
    return UnderlyingQuote(
        ticker="TESTX44",
        price=price,  # type: ignore[arg-type]
        bid=bid,
        ask=ask,
        timestamp=ts,
        market_data_quality="live",
        source_provider="ibkr",
        retrieved_at=ts,
    )


def _exit_quote(
    external_contract_id: str,
    option_type: str,
    strike: Decimal,
    bid: Decimal | None,
    ask: Decimal | None,
    *,
    ts: datetime | None = None,
    **overrides,
) -> OptionQuote:
    ts = ts or _due_now()
    defaults = dict(
        ticker="TESTX44",
        snapshot_timestamp=ts,
        expiration_date=EXP,
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        last_price=(bid + ask) / 2 if bid is not None and ask is not None else None,
        market_data_quality="live",
        external_contract_id=external_contract_id,
        source_provider="ibkr",
        retrieved_at=ts,
    )
    defaults.update(overrides)
    return OptionQuote(**defaults)  # type: ignore[arg-type]


def _seed_event_and_portfolio(
    db_session,
    symbol: str = "TESTX44",
    *,
    earnings_date: date = EARNINGS_DATE,
    earnings_time: EarningsTiming = EarningsTiming.AMC,
) -> tuple[EarningsCalendarEvent, BenchmarkPortfolio]:
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name="Phase 4.5 Test Co",
        earnings_date=earnings_date,
        earnings_time=earnings_time,
    )
    portfolio = BenchmarkPortfolio(
        name=f"Test Portfolio {symbol}",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
        risk_profile=RiskProfile.MODERATE,
    )
    db_session.add_all([event, portfolio])
    db_session.flush()
    return event, portfolio


def _seed_entered_decision(
    db_session,
    event: EarningsCalendarEvent,
    portfolio: BenchmarkPortfolio,
    legs: list[dict],
    *,
    net_entry_cash: Decimal,
    initial_max_risk: Decimal,
    contracts: int = 1,
    strategy_type: str = "long_call",
    entry_generated_at: datetime | None = None,
) -> tuple[DecisionSnapshot, EntryCaptureAttempt, list[EntrySnapshot]]:
    """Bypasses services/benchmark_entry_capture.py entirely -- directly
    constructs an already-CAPTURED entry, exactly the precondition
    capture_benchmark_exit requires, so each test can set up exactly the
    entry state it wants to exercise the exit path against."""
    schedule = compute_entry_exit_schedule(event.earnings_date, _timing_to_announcement(event))
    decision = DecisionSnapshot(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=event.symbol,
        company_name=event.company_name,
        strategy_direction=DecisionDirection.BULLISH,
        strategy_type=strategy_type,
        generated_at=entry_generated_at or schedule.entry_timestamp,
        status=DecisionSnapshotStatus.PENDING_ENTRY,
        selected_expiration=EXP,
        legs=[
            {
                "option_type": leg["option_type"],
                "action": leg["action"],
                "strike": str(leg["strike"]),
                "quantity": leg.get("quantity", 1),
            }
            for leg in legs
        ],
        engine_version="options-decision-engine-v3",
        prompt_version="v1",
        expiration_source="v3_auto_resolver",
    )
    db_session.add(decision)
    db_session.flush()

    entry_attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=CaptureStatus.CAPTURED,
        net_entry_cash=net_entry_cash,
        contracts=contracts,
        initial_max_risk=initial_max_risk,
        captured_at=schedule.entry_timestamp,
    )
    db_session.add(entry_attempt)
    db_session.flush()

    entry_legs: list[EntrySnapshot] = []
    for idx, leg in enumerate(legs):
        entry_leg = EntrySnapshot(
            decision_id=decision.id,
            capture_attempt_id=entry_attempt.id,
            leg_index=idx,
            status=CaptureStatus.CAPTURED,
            captured_at=schedule.entry_timestamp,
            external_contract_id=leg["external_contract_id"],
            expiration=EXP,
            strike=Decimal(str(leg["strike"])),
            option_type=OptionType(leg["option_type"]),
            action=OptionAction(leg["action"]),
            quantity=leg.get("quantity", 1),
            multiplier=Decimal(100),
            benchmark_entry_price=leg["entry_price"],
            pricing_assumption=(
                "BUY_TO_OPEN_AT_ASK" if leg["action"] == "buy" else "SELL_TO_OPEN_AT_BID"
            ),
            source_provider="ibkr",
        )
        db_session.add(entry_leg)
        entry_legs.append(entry_leg)
    db_session.flush()
    return decision, entry_attempt, entry_legs


def _timing_to_announcement(event: EarningsCalendarEvent) -> AnnouncementTime:
    mapping = {
        EarningsTiming.BMO: AnnouncementTime.BEFORE_MARKET,
        EarningsTiming.AMC: AnnouncementTime.AFTER_MARKET,
        EarningsTiming.DMH: AnnouncementTime.UNKNOWN,
        EarningsTiming.UNKNOWN: AnnouncementTime.UNKNOWN,
    }
    return mapping[event.earnings_time]


def _single_long_call_leg(external_contract_id: str = "111") -> list[dict]:
    return [
        {
            "option_type": "call",
            "action": "buy",
            "strike": Decimal("100"),
            "entry_price": Decimal("2.10"),  # ASK, matching BUY_TO_OPEN_AT_ASK
            "external_contract_id": external_contract_id,
        }
    ]


def _single_short_call_leg(external_contract_id: str = "222") -> list[dict]:
    return [
        {
            "option_type": "call",
            "action": "sell",
            "strike": Decimal("100"),
            "entry_price": Decimal("1.90"),  # BID, matching SELL_TO_OPEN_AT_BID
            "external_contract_id": external_contract_id,
        }
    ]


def _butterfly_legs() -> list[dict]:
    return [
        {
            "option_type": "call", "action": "buy", "strike": Decimal("95"),
            "entry_price": Decimal("6.20"), "external_contract_id": "301",
        },
        {
            "option_type": "call", "action": "sell", "strike": Decimal("100"),
            "entry_price": Decimal("2.90"), "quantity": 2, "external_contract_id": "302",
        },
        {
            "option_type": "call", "action": "buy", "strike": Decimal("105"),
            "entry_price": Decimal("1.10"), "external_contract_id": "303",
        },
    ]  # fmt: skip


# --------------------------------------------------------------------------
# Long/short fill rule
# --------------------------------------------------------------------------


def test_long_leg_uses_bid_at_exit(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"))]
    provider = _FakeExitOptionsProvider(quotes)

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    assert attempt.status == CaptureStatus.CAPTURED
    leg = db_session.query(ExitSnapshot).filter_by(settlement_attempt_id=attempt.id).one()
    assert leg.benchmark_exit_price == Decimal("2.90")  # BID for a long exit
    assert leg.pricing_assumption == "SELL_TO_CLOSE_AT_BID"
    assert leg.realized_pnl_per_share == Decimal("0.80")  # 2.90 - 2.10


def test_short_leg_uses_ask_at_exit(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_short_call_leg(),
        net_entry_cash=Decimal("-190.00"), initial_max_risk=Decimal("500.00"),
    )
    quotes = [_exit_quote("222", "call", Decimal("100"), Decimal("0.90"), Decimal("1.10"))]
    provider = _FakeExitOptionsProvider(quotes)

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    assert attempt.status == CaptureStatus.CAPTURED
    leg = db_session.query(ExitSnapshot).filter_by(settlement_attempt_id=attempt.id).one()
    assert leg.benchmark_exit_price == Decimal("1.10")  # ASK for a short exit
    assert leg.pricing_assumption == "BUY_TO_CLOSE_AT_ASK"
    assert leg.realized_pnl_per_share == Decimal("0.80")  # 1.90 - 1.10


# --------------------------------------------------------------------------
# P&L / return / R-multiple wiring (exhaustive math coverage lives in
# test_analytics_decision_settlement_math.py -- this just confirms the
# service wires the right EntryCaptureAttempt figures through unchanged).
# --------------------------------------------------------------------------


def test_realized_pnl_return_and_r_multiple_computed_from_frozen_entry_figures(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"))]
    provider = _FakeExitOptionsProvider(quotes)

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.CAPTURED
    assert attempt.realized_pnl == Decimal("80.00")  # 0.80/sh * 1 * 100 * 1
    assert attempt.return_pct == Decimal("80.00") / Decimal("210.00") * 100
    assert attempt.r_multiple == Decimal("80.00") / Decimal("210.00")
    assert attempt.is_win is True


def test_sizing_is_never_recalculated_at_settlement(db_session):
    """contracts/quantity are read verbatim from the frozen entry
    attempt/legs -- capture_benchmark_exit never calls compute_budget_fit
    (Phase 4.5 approved decision 3)."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, entry_attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("630.00"), initial_max_risk=Decimal("630.00"), contracts=3,
    )
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"))]
    provider = _FakeExitOptionsProvider(quotes)

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.CAPTURED
    assert attempt.realized_pnl == Decimal("240.00")  # 0.80/sh * 1 * 100 * 3 contracts
    assert entry_attempt.contracts == 3  # untouched


# --------------------------------------------------------------------------
# Multi-leg
# --------------------------------------------------------------------------


def test_butterfly_legs_each_get_their_own_exit_snapshot(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _butterfly_legs(),
        net_entry_cash=Decimal("100.00"), initial_max_risk=Decimal("400.00"),
        strategy_type="butterfly",
    )
    quotes = [
        _exit_quote("301", "call", Decimal("95"), Decimal("7.80"), Decimal("8.20")),
        _exit_quote("302", "call", Decimal("100"), Decimal("3.40"), Decimal("3.60")),
        _exit_quote("303", "call", Decimal("105"), Decimal("0.40"), Decimal("0.60")),
    ]
    provider = _FakeExitOptionsProvider(quotes)

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    assert attempt.status == CaptureStatus.CAPTURED
    legs = (
        db_session.query(ExitSnapshot)
        .filter_by(settlement_attempt_id=attempt.id)
        .order_by(ExitSnapshot.leg_index)
        .all()
    )
    assert len(legs) == 3
    buy_95, sell_100, buy_105 = legs
    assert buy_95.benchmark_exit_price == Decimal("7.80")  # BID for long
    assert sell_100.benchmark_exit_price == Decimal("3.60")  # ASK for short
    assert buy_105.benchmark_exit_price == Decimal("0.40")
    # 1.60/sh - 2*0.70/sh + (-0.70)/sh = 1.60 - 1.40 - 0.70 = -0.50 -> -50.00
    assert attempt.realized_pnl == Decimal("-50.00")


def test_missing_leg_quote_fails_the_whole_multi_leg_attempt(db_session):
    """Phase 4.5 all-or-nothing rule, mirroring entry's own: a 2-of-3-leg
    exit is never a real, honest closed position."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _butterfly_legs(),
        net_entry_cash=Decimal("100.00"), initial_max_risk=Decimal("400.00"),
        strategy_type="butterfly",
    )
    # the 105 strike's quote is missing entirely
    quotes = [
        _exit_quote("301", "call", Decimal("95"), Decimal("7.80"), Decimal("8.20")),
        _exit_quote("302", "call", Decimal("100"), Decimal("3.40"), Decimal("3.60")),
    ]
    provider = _FakeExitOptionsProvider(quotes)

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    assert attempt.status == CaptureStatus.FAILED
    assert "leg 2" in (attempt.capture_error or "")
    legs = db_session.query(ExitSnapshot).filter_by(settlement_attempt_id=attempt.id).all()
    assert len(legs) == 3  # every leg still recorded, for research/audit
    failed = [leg for leg in legs if leg.status == CaptureStatus.FAILED]
    assert len(failed) == 1
    assert failed[0].leg_index == 2


# --------------------------------------------------------------------------
# Timestamp coherence
# --------------------------------------------------------------------------


def test_coherent_timestamps_accepted(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    now = _due_now()
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"), ts=now)]
    provider = _FakeExitOptionsProvider(quotes, underlying=_underlying(ts=now))

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=now,
    )

    assert attempt.status == CaptureStatus.CAPTURED
    assert attempt.underlying_price == Decimal("100.00")


def test_excessive_timestamp_skew_rejected(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    now = _due_now()
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"), ts=now)]
    provider = _FakeExitOptionsProvider(
        quotes, underlying=_underlying(ts=now - timedelta(minutes=30))
    )

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=now,
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "skew" in (attempt.capture_error or "")


def test_unavailable_live_underlying_creates_failed_attempt(db_session):
    """No historical reconstruction fallback, ever (Phase 4.5 approved
    decision 1) -- a missing live underlying quote fails honestly."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"))]
    provider = _FakeExitOptionsProvider(quotes, underlying=None)

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "no live underlying quote" in (attempt.capture_error or "")
    assert attempt.underlying_price is None


# --------------------------------------------------------------------------
# No official entry to close
# --------------------------------------------------------------------------


def test_no_official_entry_fails_honestly(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = DecisionSnapshot(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=event.symbol,
        company_name=event.company_name,
        strategy_direction=DecisionDirection.BULLISH,
        strategy_type="long_call",
        generated_at=_exit_schedule().entry_timestamp,
        status=DecisionSnapshotStatus.PENDING_ENTRY,
        engine_version="options-decision-engine-v3",
        prompt_version="v1",
        expiration_source="v3_auto_resolver",
    )
    db_session.add(decision)
    db_session.flush()
    provider = _FakeExitOptionsProvider([])

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "no official" in (attempt.capture_error or "").lower()
    assert attempt.entry_capture_attempt_id is None


# --------------------------------------------------------------------------
# Retry / idempotency
# --------------------------------------------------------------------------


def test_successful_duplicate_capture_does_not_duplicate_settlement(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"))]
    provider = _FakeExitOptionsProvider(quotes)

    first = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()
    second = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert first.id == second.id
    count = (
        db_session.query(SettlementCaptureAttempt)
        .filter_by(decision_snapshot_id=decision.id, benchmark_portfolio_id=portfolio.id)
        .count()
    )
    assert count == 1


def test_failed_attempt_allows_a_new_retry_attempt(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )

    failing_provider = _FakeExitOptionsProvider(raise_exc=IBKRGatewayUnavailableError("down"))
    first = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=failing_provider, now=_due_now(),
    )
    db_session.flush()
    assert first.status == CaptureStatus.FAILED

    working_provider = _FakeExitOptionsProvider(
        [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"))]
    )
    second = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=working_provider, now=_due_now(),
    )
    db_session.flush()

    assert second.id != first.id
    assert second.status == CaptureStatus.CAPTURED
    count = (
        db_session.query(SettlementCaptureAttempt)
        .filter_by(decision_snapshot_id=decision.id, benchmark_portfolio_id=portfolio.id)
        .count()
    )
    assert count == 2


def test_failed_attempt_remains_immutable(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    provider = _FakeExitOptionsProvider(raise_exc=IBKRGatewayUnavailableError("down"))

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    attempt.status = CaptureStatus.CAPTURED
    with pytest.raises(ProgrammingError, match="insert-only"):
        db_session.flush()


def test_ibkr_gateway_unavailable_recorded_honestly(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    provider = _FakeExitOptionsProvider(raise_exc=IBKRGatewayUnavailableError("gateway down"))

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "gateway down" in (attempt.capture_error or "")
    assert db_session.query(ExitSnapshot).filter_by(settlement_attempt_id=attempt.id).count() == 0


# --------------------------------------------------------------------------
# No-lookahead / timing window
# --------------------------------------------------------------------------


def test_capture_exactly_at_scheduled_exit_accepted(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"))]
    provider = _FakeExitOptionsProvider(quotes)

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.CAPTURED


def test_capture_slightly_inside_early_tolerance_accepted(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    now = _due_now() - EXIT_EARLY_CAPTURE_TOLERANCE + timedelta(minutes=1)
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"), ts=now)]
    provider = _FakeExitOptionsProvider(quotes, underlying=_underlying(ts=now))

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=now,
    )

    assert attempt.status == CaptureStatus.CAPTURED


def test_capture_before_early_tolerance_rejected(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    now = _due_now() - EXIT_EARLY_CAPTURE_TOLERANCE - timedelta(minutes=1)
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"), ts=now)]
    provider = _FakeExitOptionsProvider(quotes, underlying=_underlying(ts=now))

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=now,
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "before the valid post-earnings exit window" in (attempt.capture_error or "")
    assert db_session.query(ExitSnapshot).filter_by(settlement_attempt_id=attempt.id).count() == 0


def test_capture_slightly_inside_late_tolerance_accepted(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    now = _due_now() + LATE_CUTOFF_GRACE - timedelta(minutes=1)
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"), ts=now)]
    provider = _FakeExitOptionsProvider(quotes, underlying=_underlying(ts=now))

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=now,
    )

    assert attempt.status == CaptureStatus.CAPTURED


def test_capture_after_late_tolerance_rejected(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    now = _due_now() + LATE_CUTOFF_GRACE + timedelta(minutes=1)
    quotes = [_exit_quote("111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"), ts=now)]
    provider = _FakeExitOptionsProvider(quotes, underlying=_underlying(ts=now))

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=now,
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "past the valid post-earnings exit window" in (attempt.capture_error or "")


# --------------------------------------------------------------------------
# AMC and BMO examples (matching PHASE4_5_SETTLEMENT_ARCHITECTURE_REVIEW.md
# sec 2's own worked examples exactly)
# --------------------------------------------------------------------------


def test_amc_example_earnings_monday_exit_tuesday(db_session):
    amc_earnings_date = date(2026, 9, 14)  # Monday
    event, portfolio = _seed_event_and_portfolio(
        db_session, symbol="TESTAMCX", earnings_date=amc_earnings_date,
        earnings_time=EarningsTiming.AMC,
    )
    schedule = compute_entry_exit_schedule(amc_earnings_date, AnnouncementTime.AFTER_MARKET)
    assert schedule.decision_generation_date == date(2026, 9, 14)  # Monday itself
    assert schedule.exit_date == date(2026, 9, 15)  # Tuesday

    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    quotes = [
        _exit_quote(
            "111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"),
            ts=schedule.exit_timestamp,
        )
    ]
    provider = _FakeExitOptionsProvider(quotes, underlying=_underlying(ts=schedule.exit_timestamp))

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=schedule.exit_timestamp,
    )

    assert attempt.status == CaptureStatus.CAPTURED


def test_bmo_example_earnings_tuesday_exit_same_tuesday(db_session):
    """BMO: exit is the earnings date itself, not a day later -- the case
    most likely to be gotten wrong by a naive "always T+1 from entry"
    assumption, since entry is Monday but exit is Tuesday (same calendar
    day as the earnings date)."""
    bmo_earnings_date = date(2026, 9, 15)  # Tuesday
    event, portfolio = _seed_event_and_portfolio(
        db_session, symbol="TESTBMOX", earnings_date=bmo_earnings_date,
        earnings_time=EarningsTiming.BMO,
    )
    schedule = compute_entry_exit_schedule(bmo_earnings_date, AnnouncementTime.BEFORE_MARKET)
    assert schedule.decision_generation_date == date(2026, 9, 14)  # Monday (previous trading day)
    assert schedule.exit_date == date(2026, 9, 15)  # Tuesday -- the earnings date itself

    decision, _attempt, _legs = _seed_entered_decision(
        db_session, event, portfolio, _single_long_call_leg(),
        net_entry_cash=Decimal("210.00"), initial_max_risk=Decimal("210.00"),
    )
    quotes = [
        _exit_quote(
            "111", "call", Decimal("100"), Decimal("2.90"), Decimal("3.10"),
            ts=schedule.exit_timestamp,
        )
    ]
    provider = _FakeExitOptionsProvider(quotes, underlying=_underlying(ts=schedule.exit_timestamp))

    attempt = capture_benchmark_exit(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=schedule.exit_timestamp,
    )

    assert attempt.status == CaptureStatus.CAPTURED
