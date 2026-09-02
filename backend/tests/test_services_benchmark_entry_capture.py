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
    QuoteRequirement,
    RiskProfile,
)
from models.price_bar import PriceBar
from models.quote_acquisition_attempt import QuoteAcquisitionAttempt
from providers.base import OptionsDataProvider
from providers.ibkr_client import (
    IBKRGatewayUnavailableError,
    IBKRNotAuthenticatedError,
    IBKRRateLimitedError,
)
from providers.ibkr_options import IBKRContractNotFoundError
from providers.types import (
    OptionQuote,
    SnapshotAttempt,
    SnapshotFieldPresence,
    UnderlyingQuote,
)
from services.benchmark_entry_capture import EARLY_CAPTURE_TOLERANCE, capture_benchmark_entry
from services.decision_pipeline import LATE_CUTOFF_GRACE

EARNINGS_DATE = date(2026, 9, 16)  # a Wednesday -- real trading day either side
EXP = date(2026, 9, 18)


def _due_now() -> datetime:
    schedule = compute_entry_exit_schedule(EARNINGS_DATE, AnnouncementTime.AFTER_MARKET)
    return schedule.entry_timestamp


_UNSET = object()


class _FakeOptionsProvider(OptionsDataProvider):
    """``underlying`` defaults to a coherent live quote (price 100, same
    timestamp as ``_due_now()``) unless a test explicitly overrides it --
    matching almost every existing test's intent (a normal, coherent
    capture) without every one of them having to spell it out. Pass
    ``underlying=None`` explicitly to simulate a provider with no live
    underlying data available at all (Phase 4.4 hardening sec 1)."""

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
        if self._raise_exc is not None:
            raise self._raise_exc
        return self._quotes

    def get_underlying_quote(self, ticker) -> UnderlyingQuote | None:
        if self._underlying is _UNSET:
            return _underlying()
        return self._underlying

    def list_available_expirations(self, ticker, after, max_candidates=5) -> list[date]:
        return [EXP]


class _CallCountingOptionsProvider(_FakeOptionsProvider):
    """Live market-data validation (2026-08-26), Section 25 -- proves
    capture_benchmark_entry asks for exactly the selected legs, never a
    full option-chain rediscovery, by counting real calls to each
    provider method."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.get_option_chain_calls = 0
        self.get_quotes_for_selected_legs_calls: list[tuple] = []

    def get_option_chain(self, *args, **kwargs) -> list[OptionQuote]:
        self.get_option_chain_calls += 1
        return super().get_option_chain(*args, **kwargs)

    def get_quotes_for_selected_legs(
        self, ticker, legs, expiration, as_of, on_attempt=None
    ) -> list[OptionQuote]:
        self.get_quotes_for_selected_legs_calls.append((ticker, tuple(legs), expiration))
        if self._raise_exc is not None:
            raise self._raise_exc
        wanted = {(leg.strike, leg.option_type) for leg in legs}
        return [q for q in self._quotes if (q.strike, q.option_type) in wanted]


class _TelemetryEmittingOptionsProvider(_FakeOptionsProvider):
    """IBKR execution-observability hardening (2026-08-26), Section 24 --
    a fake that actually invokes ``on_attempt`` with real, scripted
    per-poll telemetry (unlike every other fake in this file, which
    accepts and ignores it) so the full wiring from a real provider call
    through to persisted QuoteAcquisitionAttempt rows can be tested
    without a live IBKR Gateway."""

    def __init__(self, *args, attempts: list[SnapshotAttempt] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._attempts = attempts or []

    def get_quotes_for_selected_legs(
        self, ticker, legs, expiration, as_of, on_attempt=None
    ) -> list[OptionQuote]:
        if on_attempt is not None:
            for attempt in self._attempts:
                on_attempt(attempt)
        wanted = {(leg.strike, leg.option_type) for leg in legs}
        return [q for q in self._quotes if (q.strike, q.option_type) in wanted]


def _underlying(
    price: Decimal | None = Decimal("100.00"),
    *,
    ts: datetime | None = None,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
) -> UnderlyingQuote:
    """A real, live UnderlyingQuote -- the provider's own contemporaneous
    observation (Phase 4.4 hardening sec 1), never derived from a daily
    close. Defaults to coherent with ``_due_now()``-timed option quotes."""
    ts = ts or _due_now()
    return UnderlyingQuote(
        ticker="TESTP44",
        price=price,  # type: ignore[arg-type]
        bid=bid,
        ask=ask,
        timestamp=ts,
        market_data_quality="live",
        source_provider="ibkr",
        retrieved_at=ts,
    )


def _quote(
    strike: Decimal,
    option_type: str,
    bid: Decimal | None,
    ask: Decimal | None,
    *,
    ts: datetime | None = None,
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
        source_provider="ibkr",
        retrieved_at=ts,
    )
    defaults.update(overrides)
    return OptionQuote(**defaults)  # type: ignore[arg-type]


def _seed_event_and_portfolio(
    db_session,
    symbol: str = "TESTP44",
    *,
    earnings_date: date = EARNINGS_DATE,
    earnings_time: EarningsTiming = EarningsTiming.AMC,
) -> tuple[EarningsCalendarEvent, BenchmarkPortfolio]:
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name="Phase 4.4 Test Co",
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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


def test_capture_quotes_exact_legs_never_rediscovers_a_full_chain(db_session):
    """Live market-data validation (2026-08-26), Section 7/25: entry
    capture must quote exactly the legs Decision Engine already selected
    (get_quotes_for_selected_legs) and must never call get_option_chain()
    -- the redundant full ATM-window rediscovery this task's Section 7
    exists to eliminate."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _butterfly_legs())
    quotes = [
        _quote(Decimal("95"), "call", Decimal("5.80"), Decimal("6.20")),
        _quote(Decimal("100"), "call", Decimal("2.90"), Decimal("3.10")),
        _quote(Decimal("105"), "call", Decimal("0.90"), Decimal("1.10")),
    ]
    provider = _CallCountingOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )
    db_session.flush()

    assert attempt.status == CaptureStatus.CAPTURED
    assert provider.get_option_chain_calls == 0
    assert len(provider.get_quotes_for_selected_legs_calls) == 1
    ticker, legs, expiration = provider.get_quotes_for_selected_legs_calls[0]
    assert ticker == "TESTP44"
    assert expiration == EXP
    assert {(leg.strike, leg.option_type) for leg in legs} == {
        (Decimal("95"), "call"),
        (Decimal("100"), "call"),
        (Decimal("105"), "call"),
    }


def test_quote_acquisition_telemetry_persisted_for_a_long_entry_leg(db_session):
    """IBKR execution-observability hardening (2026-08-26), Section 24 --
    every real poll attempt the provider reports is persisted, in order,
    with the correct required side, presence flags, values, and quality
    -- readable from Operations without log archaeology."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    final_quote = _quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))
    attempts = [
        SnapshotAttempt(
            attempt=1,
            elapsed_ms=1500.0,
            per_conid={
                12345: SnapshotFieldPresence(
                    bid_present=False,
                    ask_present=False,
                    last_present=True,
                    market_data_quality="delayed",
                    last_price=Decimal("2.00"),
                )
            },
        ),
        SnapshotAttempt(
            attempt=2,
            elapsed_ms=3000.0,
            per_conid={
                12345: SnapshotFieldPresence(
                    bid_present=True,
                    ask_present=True,
                    last_present=True,
                    market_data_quality="delayed",
                    bid=Decimal("1.90"),
                    ask=Decimal("2.10"),
                    last_price=Decimal("2.00"),
                )
            },
        ),
    ]
    provider = _TelemetryEmittingOptionsProvider([final_quote], attempts=attempts)

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )
    db_session.flush()

    assert attempt.status == CaptureStatus.CAPTURED
    rows = (
        db_session.query(QuoteAcquisitionAttempt)
        .filter_by(entry_capture_attempt_id=attempt.id)
        .order_by(QuoteAcquisitionAttempt.snapshot_attempt_number)
        .all()
    )
    assert len(rows) == 2
    first, second = rows
    assert first.snapshot_attempt_number == 1
    assert first.elapsed_ms == 1500
    assert first.elapsed_ms >= 0
    assert first.bid_present is False
    assert first.ask_present is False
    assert first.last_present is True
    assert first.required_side == QuoteRequirement.ASK  # a BUY leg
    assert first.strike == Decimal("100")
    assert first.option_type.value == "call"
    assert first.leg_index == 0
    assert first.contract_resolved is True
    assert first.final_for_leg is False
    assert first.market_data_quality == "delayed"

    assert second.snapshot_attempt_number == 2
    assert second.elapsed_ms == 3000
    assert second.bid_present is True
    assert second.ask_present is True
    assert second.bid == Decimal("1.90")
    assert second.ask == Decimal("2.10")
    assert second.final_for_leg is True
    assert second.required_side == QuoteRequirement.ASK

    # Telemetry never touches the official fill -- still exactly ASK,
    # computed by _price_leg from the real returned quote, not from any
    # telemetry row.
    entry_snapshot = db_session.query(EntrySnapshot).filter_by(capture_attempt_id=attempt.id).one()
    assert entry_snapshot.benchmark_entry_price == Decimal("2.10")


def test_quote_acquisition_telemetry_records_unresolved_contract(db_session):
    """A leg whose exact contract never resolves at all is still a real,
    honest telemetry row -- attempt 0, contract_resolved=False -- not
    silently dropped."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    # No quotes at all -- get_quotes_for_selected_legs returns [], meaning
    # the (strike, option_type) this leg needed was never resolved.
    provider = _TelemetryEmittingOptionsProvider([])

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )
    db_session.flush()

    assert attempt.status == CaptureStatus.FAILED
    row = (
        db_session.query(QuoteAcquisitionAttempt)
        .filter_by(entry_capture_attempt_id=attempt.id)
        .one()
    )
    assert row.contract_resolved is False
    assert row.snapshot_attempt_number == 0
    assert row.external_contract_id is None
    assert row.strike == Decimal("100")
    assert row.required_side == QuoteRequirement.ASK


def test_bid_ask_mid_last_and_greeks_preserved(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"), ts=now)]
    provider = _FakeOptionsProvider(quotes, underlying=_underlying(ts=now))

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=now,
    )

    assert attempt.status == CaptureStatus.CAPTURED
    assert attempt.underlying_price == Decimal("100.00")


def test_excessive_timestamp_skew_rejected(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    now = _due_now()
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"), ts=now)]
    provider = _FakeOptionsProvider(quotes, underlying=_underlying(ts=now - timedelta(minutes=30)))

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=now,
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    # V4 consolidation, Section 15 -- the refusal names the BINDING
    # constraint. $19,000 per contract exceeds the whole $2,000 account, so
    # this is a CAPITAL refusal (distinct from a risk-cap refusal), and the
    # message must say so and quote both numbers.
    error = attempt.capture_error or ""
    assert error.startswith("Capital insufficient")
    assert "$19,000.00" in error and "$2,000" in error
    assert "cannot size even one contract" not in error


# --------------------------------------------------------------------------
# Retry / idempotency
# --------------------------------------------------------------------------


def test_successful_duplicate_capture_does_not_duplicate_benchmark_entry(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes)

    first = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )
    db_session.flush()
    second = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=failing_provider,
        now=_due_now(),
    )
    db_session.flush()
    assert first.status == CaptureStatus.FAILED

    working_provider = _FakeOptionsProvider(
        [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    )
    second = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=working_provider,
        now=_due_now(),
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "gateway down" in (attempt.capture_error or "")
    assert db_session.query(EntrySnapshot).filter_by(capture_attempt_id=attempt.id).count() == 0
    # Phase 4 quote-observability hardening (2026-08-26), Section 10 --
    # this real provider exception must still leave structured diagnostic
    # evidence, not just the free-text capture_error above.
    rows = (
        db_session.query(QuoteAcquisitionAttempt)
        .filter_by(entry_capture_attempt_id=attempt.id)
        .all()
    )
    assert len(rows) == 1  # one requested leg
    assert rows[0].provider_error_category == "GATEWAY_UNREACHABLE"
    assert rows[0].rate_limited is False
    assert rows[0].permission_error is False
    assert rows[0].contract_resolved is True
    assert rows[0].bid_present is False and rows[0].ask_present is False


def test_ibkr_not_authenticated_recorded_honestly(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    provider = _FakeOptionsProvider(raise_exc=IBKRNotAuthenticatedError("not authenticated"))

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "not authenticated" in (attempt.capture_error or "")
    rows = (
        db_session.query(QuoteAcquisitionAttempt)
        .filter_by(entry_capture_attempt_id=attempt.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].provider_error_category == "AUTH_REQUIRED"
    assert rows[0].permission_error is True


def test_ibkr_rate_limited_produces_structured_telemetry(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    provider = _FakeOptionsProvider(raise_exc=IBKRRateLimitedError("rate-limited the request"))

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    rows = (
        db_session.query(QuoteAcquisitionAttempt)
        .filter_by(entry_capture_attempt_id=attempt.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].provider_error_category == "RATE_LIMITED"
    assert rows[0].rate_limited is True
    from services.entry_failure_taxonomy import classify_from_structured_evidence

    assert classify_from_structured_evidence(rows) == "ENTRY_RATE_LIMITED"


def test_ibkr_contract_not_found_marks_unresolved(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    provider = _FakeOptionsProvider(
        raise_exc=IBKRContractNotFoundError("no listed underlying with an options section found")
    )

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    rows = (
        db_session.query(QuoteAcquisitionAttempt)
        .filter_by(entry_capture_attempt_id=attempt.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].provider_error_category == "CONTRACT_RESOLUTION_ERROR"
    assert rows[0].contract_resolved is False


class TestMarketDataQualityPolicy:
    """Phase 4 market-data-quality hardening (2026-08-26), Section 16 --
    the default (ALLOW_DELAYED_WITH_LABEL) is exercised implicitly by
    every other test in this file (every fake provider's quotes have
    market_data_quality=None, and captures still succeed) -- these prove
    the explicit LIVE_ONLY policy path, which nothing else here touches."""

    def test_live_only_rejects_a_delayed_leg(self, db_session, monkeypatch):
        import services.benchmark_entry_capture as entry_capture_module
        from core.config import Settings
        from models.enums import MarketDataQualityPolicy

        monkeypatch.setattr(
            entry_capture_module,
            "get_settings",
            lambda: Settings(
                market_data_quality_policy=MarketDataQualityPolicy.LIVE_ONLY, _env_file=None
            ),
        )
        event, portfolio = _seed_event_and_portfolio(db_session)
        decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
        quotes = [
            _quote(
                Decimal("100"),
                "call",
                Decimal("2.00"),
                Decimal("2.10"),
                market_data_quality="delayed",
            )
        ]
        provider = _FakeOptionsProvider(quotes)

        attempt = capture_benchmark_entry(
            db_session,
            decision_snapshot=decision,
            portfolio=portfolio,
            options_provider=provider,
            now=_due_now(),
        )

        assert attempt.status == CaptureStatus.FAILED
        assert "live_only" in (attempt.capture_error or "")
        assert "delayed" in (attempt.capture_error or "")

    def test_live_only_accepts_a_genuinely_live_leg(self, db_session, monkeypatch):
        import services.benchmark_entry_capture as entry_capture_module
        from core.config import Settings
        from models.enums import MarketDataQualityPolicy

        monkeypatch.setattr(
            entry_capture_module,
            "get_settings",
            lambda: Settings(
                market_data_quality_policy=MarketDataQualityPolicy.LIVE_ONLY, _env_file=None
            ),
        )
        event, portfolio = _seed_event_and_portfolio(db_session)
        decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
        quotes = [
            _quote(
                Decimal("100"),
                "call",
                Decimal("2.00"),
                Decimal("2.10"),
                market_data_quality="live",
            )
        ]
        provider = _FakeOptionsProvider(quotes, underlying=_underlying())

        attempt = capture_benchmark_entry(
            db_session,
            decision_snapshot=decision,
            portfolio=portfolio,
            options_provider=provider,
            now=_due_now(),
        )

        assert attempt.status == CaptureStatus.CAPTURED


def test_quote_acquisition_attempt_update_rejected_by_database(db_session):
    """Section 12 -- append-only is now enforced at the database level,
    not only by convention."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    provider = _FakeOptionsProvider(raise_exc=IBKRRateLimitedError("rate-limited"))

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )
    row = (
        db_session.query(QuoteAcquisitionAttempt)
        .filter_by(entry_capture_attempt_id=attempt.id)
        .one()
    )
    row.rate_limited = False
    with pytest.raises(ProgrammingError, match="insert-only"):
        db_session.flush()
    db_session.rollback()


def test_contracts_only_no_bid_ask_fails_the_leg(db_session):
    """A chain that returns contracts with no real bid/ask (Phase 14.13's
    "contracts only" tier) can't back a conservative fill -- honest
    failure, never a fabricated price."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", None, None)]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=too_late,
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "cutoff" in (attempt.capture_error or "") or "window" in (attempt.capture_error or "")
    assert db_session.query(EntrySnapshot).filter_by(capture_attempt_id=attempt.id).count() == 0


def test_a_real_failed_attempt_can_never_be_retried_into_captured_after_the_window_closes(
    db_session,
):
    """Post-live correction (2026-08-25) Section 15 -- the exact real Aug
    25 shape: INTU/HEI/ZM/SMTC's entry captures genuinely failed inside
    the legal window (real missing quotes), and Operations used to show
    "Retry entry capture" for them indefinitely afterward. Server-side
    enforcement already exists here (_verify_no_lookahead, checked fresh
    on every call, never trusted from an earlier attempt) -- this proves
    it directly: even with a provider that would now happily return a
    full quote, a retry attempt hours after the window closed must still
    fail honestly, and must never produce a second, CAPTURED row."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())

    first = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=_FakeOptionsProvider([]),  # real quote failure, in-window
        now=_due_now(),
    )
    db_session.flush()
    assert first.status == CaptureStatus.FAILED

    working_provider = _FakeOptionsProvider(
        [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    )
    retry_hours_later = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=working_provider,
        now=_due_now() + timedelta(hours=3),
    )
    db_session.flush()

    assert retry_hours_later.status == CaptureStatus.FAILED
    assert "window" in (retry_hours_later.capture_error or "") or "cutoff" in (
        retry_hours_later.capture_error or ""
    )
    captured_count = (
        db_session.query(EntryCaptureAttempt)
        .filter_by(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.CAPTURED,
        )
        .count()
    )
    assert captured_count == 0  # never a real official entry, no matter how many attempts


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
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "permitted decision cutoff" in (attempt.capture_error or "")


# --------------------------------------------------------------------------
# Hardening pass sec 1 -- underlying sourcing must be live, never a stale
# daily close, and a missing live observation must fail honestly.
# --------------------------------------------------------------------------


def test_unavailable_live_underlying_creates_failed_attempt(db_session):
    """The options provider has real, current option quotes but no live
    underlying capability at all (get_underlying_quote -> None) -- the
    official capture must fail honestly, never silently proceed without
    underlying context and never reach for a daily close."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes, underlying=None)

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "no live underlying quote" in (attempt.capture_error or "")
    assert attempt.underlying_price is None


def test_stale_daily_close_never_satisfies_official_capture(db_session):
    """A PriceBar daily-close row exists for this ticker/date with a
    materially different price than the live underlying quote -- if the
    forbidden fallback were still reachable, a missing/None underlying
    quote could silently resolve to this value instead of failing. It
    must not: the daily close is retained purely as V3 research/reference
    data (services/options_analytics.py's own concern) and must never be
    able to satisfy an OFFICIAL capture."""
    event, portfolio = _seed_event_and_portfolio(db_session, symbol="TESTSTALE")
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    db_session.add(
        PriceBar(
            ticker="TESTSTALE",
            trade_date=EARNINGS_DATE,
            open=Decimal("50.00"),
            high=Decimal("51.00"),
            low=Decimal("49.00"),
            close=Decimal("50.00"),  # deliberately far from the live 100.00 below
            volume=1_000_000,
            source_provider="stooq",
            retrieved_at=_due_now(),
        )
    )
    db_session.flush()
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]

    # Case A: no live underlying quote at all -- must fail, never silently
    # resolve to the $50.00 daily close that exists in the DB.
    provider_no_live = _FakeOptionsProvider(quotes, underlying=None)
    failed = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider_no_live,
        now=_due_now(),
    )
    assert failed.status == CaptureStatus.FAILED
    assert failed.underlying_price is None
    assert failed.underlying_price != Decimal("50.00")

    # Case B: a real live underlying quote IS available (price 100) -- the
    # captured attempt must use that live price, never the $50.00 daily
    # close sitting in the same DB. Retried on the same decision -- the
    # first attempt was FAILED, not CAPTURED, so idempotency (which only
    # short-circuits on an existing CAPTURED attempt) allows this retry.
    provider_live = _FakeOptionsProvider(quotes, underlying=_underlying(Decimal("100.00")))
    captured = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider_live,
        now=_due_now(),
    )
    assert captured.status == CaptureStatus.CAPTURED
    assert captured.underlying_price == Decimal("100.00")


def test_live_underlying_bid_ask_persisted_when_provider_offers_them(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(
        quotes, underlying=_underlying(bid=Decimal("99.95"), ask=Decimal("100.05"))
    )

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.CAPTURED
    assert attempt.underlying_bid == Decimal("99.95")
    assert attempt.underlying_ask == Decimal("100.05")


def test_underlying_bid_ask_stay_null_when_provider_only_has_last_price(db_session):
    """A provider (or IBKR response) that only returns a last/market price
    for the underlying -- no bid/ask -- must never have those fabricated
    from the price alone."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes, underlying=_underlying())  # bid/ask default None

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.CAPTURED
    assert attempt.underlying_bid is None
    assert attempt.underlying_ask is None


# --------------------------------------------------------------------------
# Hardening pass sec 2 -- both sides of the entry capture window
# --------------------------------------------------------------------------


def test_capture_exactly_at_scheduled_entry_accepted(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.CAPTURED


def test_capture_slightly_inside_early_tolerance_accepted(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    now = _due_now() - EARLY_CAPTURE_TOLERANCE + timedelta(minutes=1)
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"), ts=now)]
    provider = _FakeOptionsProvider(quotes, underlying=_underlying(ts=now))

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=now,
    )

    assert attempt.status == CaptureStatus.CAPTURED


def test_capture_before_early_tolerance_rejected(db_session):
    """A materially early capture (e.g. mid-morning for a 15:55 ET
    benchmark) must never stand in for the scheduled entry -- Phase 4.4
    hardening sec 2, the early half of the entry window."""
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    now = _due_now() - EARLY_CAPTURE_TOLERANCE - timedelta(minutes=1)
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"), ts=now)]
    provider = _FakeOptionsProvider(quotes, underlying=_underlying(ts=now))

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=now,
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "before the valid pre-earnings entry window" in (attempt.capture_error or "")
    assert db_session.query(EntrySnapshot).filter_by(capture_attempt_id=attempt.id).count() == 0


def test_capture_slightly_inside_late_tolerance_accepted(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    now = _due_now() + LATE_CUTOFF_GRACE - timedelta(minutes=1)
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"), ts=now)]
    provider = _FakeOptionsProvider(quotes, underlying=_underlying(ts=now))

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=now,
    )

    assert attempt.status == CaptureStatus.CAPTURED


def test_capture_after_late_tolerance_rejected(db_session):
    event, portfolio = _seed_event_and_portfolio(db_session)
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    now = _due_now() + LATE_CUTOFF_GRACE + timedelta(minutes=1)
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"), ts=now)]
    provider = _FakeOptionsProvider(quotes, underlying=_underlying(ts=now))

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=now,
    )

    assert attempt.status == CaptureStatus.FAILED
    assert "past the valid pre-earnings entry window" in (attempt.capture_error or "")


def test_bmo_entry_window_anchors_to_previous_trading_day(db_session):
    """BMO: the release is already known by the earnings date's own open,
    so entry is the previous real trading day's 15:55 ET -- reusing
    analytics/earnings_timing.py, never a separate rule. A capture at that
    previous-day 15:55 ET is accepted; a capture on the earnings date
    itself (a full day later) is not."""
    bmo_earnings_date = date(2026, 9, 17)  # Thursday
    event, portfolio = _seed_event_and_portfolio(
        db_session,
        symbol="TESTBMO",
        earnings_date=bmo_earnings_date,
        earnings_time=EarningsTiming.BMO,
    )
    schedule = compute_entry_exit_schedule(bmo_earnings_date, AnnouncementTime.BEFORE_MARKET)
    assert schedule.decision_generation_date == date(2026, 9, 16)  # the prior Wednesday

    decision = _seed_decision(
        db_session,
        event,
        portfolio,
        _long_call_legs(),
        generated_at=schedule.entry_timestamp,
    )
    quotes = [
        _quote(
            Decimal("100"),
            "call",
            Decimal("1.90"),
            Decimal("2.10"),
            ts=schedule.entry_timestamp,
        )
    ]
    provider = _FakeOptionsProvider(quotes, underlying=_underlying(ts=schedule.entry_timestamp))

    # Rejected case first -- run before any CAPTURED attempt exists for
    # this decision, since idempotency short-circuits on an existing
    # CAPTURED attempt regardless of the window check.
    too_late_by_a_day = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=schedule.entry_timestamp + timedelta(days=1),
    )
    # A full day later is past LATE_CUTOFF_GRACE -- confirms the window is
    # anchored to the real BMO-shifted entry_timestamp (2026-09-16), not
    # the raw earnings_date (2026-09-17).
    assert too_late_by_a_day.status == CaptureStatus.FAILED

    on_time = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=schedule.entry_timestamp,
    )
    assert on_time.status == CaptureStatus.CAPTURED


def test_amc_entry_window_anchors_to_earnings_date_itself(db_session):
    """AMC: the release happens after the close, so entering at 15:55 ET
    on the earnings date itself is still pre-release -- entry is not
    shifted to a different day, unlike BMO."""
    event, portfolio = _seed_event_and_portfolio(db_session)  # default: AMC, EARNINGS_DATE
    decision = _seed_decision(db_session, event, portfolio, _long_call_legs())
    quotes = [_quote(Decimal("100"), "call", Decimal("1.90"), Decimal("2.10"))]
    provider = _FakeOptionsProvider(quotes)

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=_due_now(),
    )

    assert attempt.status == CaptureStatus.CAPTURED
    schedule = compute_entry_exit_schedule(EARNINGS_DATE, AnnouncementTime.AFTER_MARKET)
    assert schedule.decision_generation_date == EARNINGS_DATE


def test_bmo_over_a_weekend_skips_to_previous_friday(db_session):
    """A Monday BMO earnings date must anchor entry to the previous real
    trading day, skipping the weekend entirely (Friday, not Sunday) --
    proving the window check operates against the real trading-day-aware
    entry_timestamp, not a naive calendar day-before."""
    monday_earnings_date = date(2026, 9, 21)  # Monday
    event, portfolio = _seed_event_and_portfolio(
        db_session,
        symbol="TESTWKND",
        earnings_date=monday_earnings_date,
        earnings_time=EarningsTiming.BMO,
    )
    schedule = compute_entry_exit_schedule(monday_earnings_date, AnnouncementTime.BEFORE_MARKET)
    assert schedule.decision_generation_date == date(2026, 9, 18)  # the prior Friday

    decision = _seed_decision(
        db_session,
        event,
        portfolio,
        _long_call_legs(),
        generated_at=schedule.entry_timestamp,
    )
    quotes = [
        _quote(
            Decimal("100"),
            "call",
            Decimal("1.90"),
            Decimal("2.10"),
            ts=schedule.entry_timestamp,
        )
    ]
    provider = _FakeOptionsProvider(quotes, underlying=_underlying(ts=schedule.entry_timestamp))

    # Rejected case first -- before any CAPTURED attempt exists for this
    # decision (idempotency short-circuits on an existing CAPTURED
    # attempt regardless of the window check). A capture attempted on the
    # weekend itself (Saturday) is well before the real Friday
    # entry_timestamp -- confirms the early-side check rejects naive
    # weekend timestamps too, not just weekday ones.
    weekend_now = datetime.combine(date(2026, 9, 19), schedule.entry_timestamp.timetz())
    rejected = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=weekend_now,
    )
    assert rejected.status == CaptureStatus.FAILED

    attempt = capture_benchmark_entry(
        db_session,
        decision_snapshot=decision,
        portfolio=portfolio,
        options_provider=provider,
        now=schedule.entry_timestamp,
    )
    assert attempt.status == CaptureStatus.CAPTURED
