"""Phase 4.4 -- unit tests for services/benchmark_entry_capture.py. Uses
an in-memory fake OptionsDataProvider, never a live provider call."""

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
    RiskProfile,
)
from providers.base import OptionsDataProvider
from providers.ibkr_client import IBKRGatewayUnavailableError, IBKRNotAuthenticatedError
from providers.types import OptionQuote
from services.benchmark_entry_capture import capture_benchmark_entry

EARNINGS_DATE = date(2026, 9, 16)  # a Wednesday -- real trading day either side
EXP = date(2026, 9, 18)


def _due_now() -> datetime:
    schedule = compute_entry_exit_schedule(EARNINGS_DATE, AnnouncementTime.AFTER_MARKET)
    return schedule.entry_timestamp


class _FakeOptionsProvider(OptionsDataProvider):
    def __init__(self, quotes: list[OptionQuote] | None = None, raise_exc: Exception | None = None):
        self._quotes = quotes if quotes is not None else []
        self._raise_exc = raise_exc

    def get_option_chain(
        self, ticker, as_of, expiration=None, reference_date=None, earnings_anchored=True
    ) -> list[OptionQuote]:
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._quotes

    def list_available_expirations(self, ticker, after, max_candidates=5) -> list[date]:
        return [EXP]


def _quote(
    strike: Decimal,
    option_type: str,
    bid: Decimal | None,
    ask: Decimal | None,
    *,
    ts: datetime | None = None,
    underlying_price: Decimal | None = Decimal("100.00"),
    underlying_timestamp: datetime | None = None,
    **overrides,
) -> OptionQuote:
    ts = ts or _due_now()
    defaults = dict(
        ticker="TESTP44",
        snapshot_timestamp=ts,
        expiration_date=EXP,
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        last_price=(bid + ask) / 2 if bid is not None and ask is not None else None,
        implied_volatility=Decimal("0.45"),
        delta=Decimal("0.5"),
        gamma=Decimal("0.05"),
        theta=Decimal("-0.02"),
        vega=Decimal("0.10"),
        market_data_quality="live",
        external_contract_id="12345",
        underlying_price=underlying_price,
        underlying_timestamp=underlying_timestamp if underlying_timestamp is not None else ts,
        source_provider="ibkr",
        retrieved_at=ts,
    )
    defaults.update(overrides)
    return OptionQuote(**defaults)  # type: ignore[arg-type]


def _seed_event_and_portfolio(
    db_session, symbol: str = "TESTP44"
) -> tuple[EarningsCalendarEvent, BenchmarkPortfolio]:
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name="Phase 4.4 Test Co",
        earnings_date=EARNINGS_DATE,
        earnings_time=EarningsTiming.AMC,
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


def _seed_decision(
    db_session,
    event: EarningsCalendarEvent,
    portfolio: BenchmarkPortfolio,
    legs: list[dict],
    *,
    strategy_type: str = "long_call",
    generated_at: datetime | None = None,
) -> DecisionSnapshot:
    decision = DecisionSnapshot(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=event.symbol,
        company_name=event.company_name,
        strategy_direction=DecisionDirection.BULLISH,
        strategy_type=strategy_type,
        generated_at=generated_at or _due_now(),
        status=DecisionSnapshotStatus.PENDING_ENTRY,
        selected_expiration=EXP,
        legs=legs,
        engine_version="options-decision-engine-v3",
        prompt_version="v1",
        expiration_source="v3_auto_resolver",
    )
    db_session.add(decision)
    db_session.flush()
    return decision


def _long_call_legs() -> list[dict]:
    return [
        {"option_type": "call", "action": "buy", "strike": "100", "premium": "2.00", "quantity": 1}
    ]


def _butterfly_legs() -> list[dict]:
    # 1 / 2 / 1 long call butterfly
    return [
        {"option_type": "call", "action": "buy", "strike": "95", "premium": "6.00", "quantity": 1},
        {
            "option_type": "call",
            "action": "sell",
            "strike": "100",
            "premium": "3.00",
            "quantity": 2,
        },
        {"option_type": "call", "action": "buy", "strike": "105", "premium": "1.00", "quantity": 1},
    ]


def _iron_condor_legs() -> list[dict]:
    return [
        {"option_type": "put", "action": "buy", "strike": "85", "premium": "0.50", "quantity": 1},
        {"option_type": "put", "action": "sell", "strike": "90", "premium": "1.00", "quantity": 1},
        {
            "option_type": "call",
            "action": "sell",
            "strike": "110",
            "premium": "1.00",
            "quantity": 1,
        },
        {"option_type": "call", "action": "buy", "strike": "115", "premium": "0.50", "quantity": 1},
    ]


# --------------------------------------------------------------------------
# Long/short fill rule, raw quote preservation
# --------------------------------------------------------------------------


def test_long_leg_uses_ask_short_leg_uses_bid(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _butterfly_legs())
    quotes = [
        _quote(Decimal("95"), "call", Decimal("5.80"), Decimal("6.20")),
        _quote(Decimal("100"), "call", Decimal("2.90"), Decimal("3.10")),
        _quote(Decimal("105"), "call", Decimal("0.90"), Decimal("1.10")),
    ]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    assert attempt.status == CaptureStatus.CAPTURED
    legs = (
        db_session.query(EntrySnapshot)
        .filter_by(capture_attempt_id=attempt.id)
        .order_by(EntrySnapshot.leg_index)
        .all()
    )
    assert len(legs) == 3
    buy_95, sell_100, buy_105 = legs
    assert buy_95.benchmark_entry_price == Decimal("6.20")  # ASK for BUY
    assert buy_95.pricing_assumption == "BUY_TO_OPEN_AT_ASK"
    assert sell_100.benchmark_entry_price == Decimal("2.90")  # BID for SELL
    assert sell_100.pricing_assumption == "SELL_TO_OPEN_AT_BID"
    assert buy_105.benchmark_entry_price == Decimal("1.10")


def test_bid_ask_mid_last_and_greeks_preserved(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    leg = db_session.query(EntrySnapshot).filter_by(capture_attempt_id=attempt.id).one()
    assert leg.bid == Decimal("1.90")
    assert leg.ask == Decimal("2.10")
    assert leg.mid == Decimal("2.00")
    assert leg.last_price == Decimal("2.00")
    assert leg.implied_volatility == Decimal("0.45")
    assert leg.delta == Decimal("0.5")
    assert leg.gamma == Decimal("0.05")
    assert leg.theta == Decimal("-0.02")
    assert leg.vega == Decimal("0.10")
    assert leg.external_contract_id == "12345"


# --------------------------------------------------------------------------
# Timestamp coherence
# --------------------------------------------------------------------------


def test_coherent_timestamps_accepted(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    now = _due_now()
    quotes = [
        _quote(
            Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"),
            ts=now, underlying_timestamp=now,
        )
    ]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=now,
    )

    assert attempt.status == CaptureStatus.CAPTURED
    assert attempt.underlying_price == Decimal("100.00")


def test_excessive_timestamp_skew_rejected(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    now = _due_now()
    quotes = [
        _quote(
            Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"),
            ts=now, underlying_timestamp=now - timedelta(minutes=30),
        )
    ]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=now,
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "skew" in (attempt.capture_error or "")


def test_missing_required_leg_rejects_entire_official_entry(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _butterfly_legs())
    # only 2 of 3 legs quoted -- the 105 strike is missing entirely
    quotes = [
        _quote(Decimal("95"), "call", Decimal("5.80"), Decimal("6.20")),
        _quote(Decimal("100"), "call", Decimal("2.90"), Decimal("3.10")),
    ]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    assert attempt.status == CaptureStatus.FAILED
    assert "leg 2" in (attempt.capture_error or "")
    legs = db_session.query(EntrySnapshot).filter_by(capture_attempt_id=attempt.id).all()
    assert len(legs) == 3  # every leg still recorded, for research/audit
    failed = [leg for leg in legs if leg.status == CaptureStatus.FAILED]
    assert len(failed) == 1
    assert failed[0].leg_index == 2


# --------------------------------------------------------------------------
# Multi-leg grouping and quantities
# --------------------------------------------------------------------------


def test_butterfly_legs_share_capture_attempt_and_preserve_quantities(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _butterfly_legs())
    quotes = [
        _quote(Decimal("95"), "call", Decimal("5.80"), Decimal("6.20")),
        _quote(Decimal("100"), "call", Decimal("2.90"), Decimal("3.10")),
        _quote(Decimal("105"), "call", Decimal("0.90"), Decimal("1.10")),
    ]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    legs = (
        db_session.query(EntrySnapshot)
        .filter_by(capture_attempt_id=attempt.id)
        .order_by(EntrySnapshot.leg_index)
        .all()
    )
    assert {leg.capture_attempt_id for leg in legs} == {attempt.id}
    assert [leg.quantity for leg in legs] == [1, 2, 1]


def test_iron_condor_legs_preserve_1_1_1_1_quantities(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session, symbol="TESTIC44")
    decision = _seed_decision(
        db_session, event, portfolio, _iron_condor_legs(), strategy_type="iron_condor"
    )
    quotes = [
        _quote(Decimal("85"), "put", Decimal("0.40"), Decimal("0.60")),
        _quote(Decimal("90"), "put", Decimal("0.90"), Decimal("1.10")),
        _quote(Decimal("110"), "call", Decimal("0.90"), Decimal("1.10")),
        _quote(Decimal("115"), "call", Decimal("0.40"), Decimal("0.60")),
    ]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    legs = (
        db_session.query(EntrySnapshot)
        .filter_by(capture_attempt_id=attempt.id)
        .order_by(EntrySnapshot.leg_index)
        .all()
    )
    assert [leg.quantity for leg in legs] == [1, 1, 1, 1]
    assert attempt.status == CaptureStatus.CAPTURED


# --------------------------------------------------------------------------
# $2,000 Moderate sizing
# --------------------------------------------------------------------------


def test_2000_moderate_sizing_within_policy(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.CAPTURED
    assert attempt.contracts is not None and attempt.contracts >= 1
    assert attempt.initial_max_risk is not None
    # Moderate = 30% of $2,000 = $600 usable risk budget (analytics/decision/risk_profile.py)
    assert attempt.initial_max_risk <= Decimal("600.00")
    assert attempt.capital_utilization is not None
    assert attempt.capital_utilization <= 100


def test_sizing_never_produces_negative_remaining_budget_or_over_100_pct(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    # A deliberately expensive contract relative to the budget -- still
    # sizeable (ask=190 -> $19,000/contract > $600 usable risk budget is
    # actually infeasible; use a smaller but still meaningful premium so
    # sizing produces a real, checkable quantity instead of "infeasible".
    quotes = [_quote(Decimal("100"), "call", Decimal("5.00"), Decimal("5.50"))]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    if attempt.status == CaptureStatus.CAPTURED:
        assert attempt.capital_utilization is not None
        assert attempt.capital_utilization <= 100
        assert attempt.initial_max_risk is not None
        assert attempt.initial_max_risk <= portfolio.cash_balance


def test_infeasible_sizing_fails_honestly(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    # $190/contract at 100x multiplier = $19,000 max loss for 1 contract,
    # far beyond Moderate's $600 usable risk budget on $2,000 -- zero
    # feasible quantity.
    quotes = [_quote(Decimal("100"), "call", Decimal("189.00"), Decimal("190.00"))]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "budget" in (attempt.capture_error or "").lower()


# --------------------------------------------------------------------------
# Retry / idempotency
# --------------------------------------------------------------------------


def test_successful_duplicate_capture_does_not_duplicate_benchmark_entry(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes)

    first = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()
    second = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert first.id == second.id
    count = (
        db_session.query(EntryCaptureAttempt)
        .filter_by(decision_snapshot_id=decision.id, benchmark_portfolio_id=portfolio.id)
        .count()
    )
    assert count == 1


def test_failed_attempt_allows_a_new_retry_attempt(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())

    failing_provider = _FakeOptionsProvider(raise_exc=IBKRGatewayUnavailableError("down"))
    first = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=failing_provider, now=_due_now(),
    )
    db_session.flush()
    assert first.status == CaptureStatus.FAILED

    working_provider = _FakeOptionsProvider(
        [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    )
    second = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=working_provider, now=_due_now(),
    )
    db_session.flush()

    assert second.id != first.id
    assert second.status == CaptureStatus.CAPTURED
    count = (
        db_session.query(EntryCaptureAttempt)
        .filter_by(decision_snapshot_id=decision.id, benchmark_portfolio_id=portfolio.id)
        .count()
    )
    assert count == 2


def test_failed_attempt_remains_immutable(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    provider = _FakeOptionsProvider(raise_exc=IBKRGatewayUnavailableError("down"))

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )
    db_session.flush()

    attempt.status = CaptureStatus.CAPTURED
    with pytest.raises(ProgrammingError, match="insert-only"):
        db_session.flush()


# --------------------------------------------------------------------------
# Provider failure
# --------------------------------------------------------------------------


def test_ibkr_gateway_unavailable_recorded_honestly(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    provider = _FakeOptionsProvider(raise_exc=IBKRGatewayUnavailableError("gateway down"))

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "gateway down" in (attempt.capture_error or "")
    assert db_session.query(EntrySnapshot).filter_by(capture_attempt_id=attempt.id).count() == 0


def test_ibkr_not_authenticated_recorded_honestly(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    provider = _FakeOptionsProvider(raise_exc=IBKRNotAuthenticatedError("not authenticated"))

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "not authenticated" in (attempt.capture_error or "")


def test_contracts_only_no_bid_ask_fails_the_leg(db_session):
    """A chain that returns contracts with no real bid/ask (Phase 14.13's
    "contracts only" tier) can't back a conservative fill -- honest
    failure, never a fabricated price."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", None, None)]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "no ask quote" in (attempt.capture_error or "")


def test_no_usable_bid_ask_for_short_leg_fails(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    short_leg = {
        "option_type": "call",
        "action": "sell",
        "strike": "100",
        "premium": "2.00",
        "quantity": 1,
    }
    decision = _seed_decision(db_session, event, portfolio, [short_leg])
    quotes = [_quote(Decimal("100"), "call", None, Decimal("2.10"))]  # no bid
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "no bid quote" in (attempt.capture_error or "")


# --------------------------------------------------------------------------
# No-lookahead / timing
# --------------------------------------------------------------------------


def test_late_scheduler_execution_rejected(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes)

    too_late = _due_now() + timedelta(hours=3)
    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=too_late,
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "cutoff" in (attempt.capture_error or "") or "window" in (attempt.capture_error or "")
    assert db_session.query(EntrySnapshot).filter_by(capture_attempt_id=attempt.id).count() == 0


def test_late_generated_decision_rejected_even_if_capture_runs_on_time(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    # decision itself claims to have been generated well past the safe
    # window -- should never happen given Phase 4.3's own guard, but
    # re-verified here rather than trusted blindly (Phase 4.4 sec 3).
    late_generated_at = _due_now() + timedelta(hours=2)
    decision = _seed_decision(
        db_session, event, portfolio, _long_call_legs(), generated_at=late_generated_at
    )
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session, decision_snapshot=decision, portfolio=portfolio,
        options_provider=provider, now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "permitted decision cutoff" in (attempt.capture_error or "")
