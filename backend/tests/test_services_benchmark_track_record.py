"""Phase 4.6 -- integration tests for services/benchmark_track_record.py.
Seeds real DecisionSnapshot/EntryCaptureAttempt/EntrySnapshot/
SettlementCaptureAttempt chains directly (bypassing the capture services
themselves, exactly like test_services_benchmark_exit_capture.py's own
_seed_entered_decision does) so each test controls exactly the settled
state it wants to exercise the aggregation engine against."""

from datetime import UTC, date, datetime
from decimal import Decimal

from models.benchmark_portfolio import BenchmarkPortfolio
from models.company import Company
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.entry_snapshot import EntrySnapshot
from models.enums import (
    CaptureStatus,
    DecisionDirection,
    DecisionSnapshotStatus,
    EarningsTiming,
    OptionAction,
    OptionType,
    RiskProfile,
)
from models.settlement_capture_attempt import SettlementCaptureAttempt
from models.volatility_snapshot import VolatilitySnapshot
from services.benchmark_track_record import (
    TrackRecordFilters,
    compute_benchmark_calibration,
    compute_benchmark_track_record,
    resolve_portfolio,
)

EARNINGS_DATE = date(2026, 9, 16)
EXP = date(2026, 9, 18)  # 2 real days after EARNINGS_DATE -> DTE bucket "0-3"


def _seed_portfolio(db_session, symbol_prefix: str = "TESTTR46") -> BenchmarkPortfolio:
    portfolio = BenchmarkPortfolio(
        name=f"Track Record Test Portfolio {symbol_prefix}",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
        risk_profile=RiskProfile.MODERATE,
        is_active=True,
    )
    db_session.add(portfolio)
    db_session.flush()
    return portfolio


def _seed_settled_decision(
    db_session,
    portfolio: BenchmarkPortfolio,
    symbol: str,
    *,
    strategy_direction: DecisionDirection = DecisionDirection.BULLISH,
    strategy_type: str = "long_call",
    estimated_probability: Decimal | None = Decimal("0.65"),
    volatility_regime: str | None = "normal",
    selected_expiration: date | None = EXP,
    entry_underlying_price: Decimal = Decimal("100.00"),
    exit_underlying_price: Decimal = Decimal("105.00"),
    realized_pnl: Decimal = Decimal("80.00"),
    r_multiple: Decimal | None = Decimal("0.5"),
    net_entry_cash: Decimal = Decimal("210.00"),
    initial_max_risk: Decimal = Decimal("210.00"),
    is_win: bool = True,
    settled_at: datetime = datetime(2026, 9, 17, 15, 55, tzinfo=UTC),
    settlement_status: CaptureStatus = CaptureStatus.CAPTURED,
    entry_status: CaptureStatus = CaptureStatus.CAPTURED,
    leg_action: OptionAction = OptionAction.BUY,
    leg_strike: Decimal = Decimal("100.00"),
    leg_entry_price: Decimal = Decimal("2.10"),
    implied_move_pct: Decimal | None = None,
) -> tuple[DecisionSnapshot, EntryCaptureAttempt | None, SettlementCaptureAttempt | None]:
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Co",
        earnings_date=EARNINGS_DATE,
        earnings_time=EarningsTiming.AMC,
    )
    db_session.add(event)
    db_session.flush()

    volatility_snapshot_id = None
    if implied_move_pct is not None:
        # cik is varchar(10) -- zero-padded to exactly 10 digits from
        # event.id rather than a fixed-prefix string, which can overflow
        # once enough real/test rows have accumulated in the shared dev
        # DB and event.id grows past a couple of digits.
        company = Company(ticker=symbol, name=f"{symbol} Co", cik=str(event.id).zfill(10)[-10:])
        db_session.add(company)
        db_session.flush()
        vol_snapshot = VolatilitySnapshot(
            company_id=company.id,
            snapshot_timestamp=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            method="test",
            implied_move_pct=implied_move_pct,
            computed_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
        )
        db_session.add(vol_snapshot)
        db_session.flush()
        volatility_snapshot_id = vol_snapshot.id

    decision = DecisionSnapshot(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=symbol,
        company_name=event.company_name,
        strategy_direction=strategy_direction,
        strategy_type=strategy_type,
        generated_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
        status=DecisionSnapshotStatus.PENDING_ENTRY,
        selected_expiration=selected_expiration,
        estimated_probability=estimated_probability,
        volatility_regime=volatility_regime,
        option_snapshot_reference=volatility_snapshot_id,
        engine_version="options-decision-engine-v3",
        prompt_version="v1",
        expiration_source="v3_auto_resolver",
    )
    db_session.add(decision)
    db_session.flush()

    entry_attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=entry_status,
        underlying_price=entry_underlying_price,
        net_entry_cash=net_entry_cash,
        contracts=1,
        initial_max_risk=initial_max_risk,
        captured_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
    )
    db_session.add(entry_attempt)
    db_session.flush()

    if entry_status == CaptureStatus.CAPTURED:
        db_session.add(
            EntrySnapshot(
                decision_id=decision.id,
                capture_attempt_id=entry_attempt.id,
                leg_index=0,
                status=CaptureStatus.CAPTURED,
                strike=leg_strike,
                option_type=OptionType.CALL,
                action=leg_action,
                quantity=1,
                multiplier=Decimal("100"),
                benchmark_entry_price=leg_entry_price,
                pricing_assumption=(
                    "BUY_TO_OPEN_AT_ASK"
                    if leg_action == OptionAction.BUY
                    else "SELL_TO_OPEN_AT_BID"
                ),
            )
        )
        db_session.flush()

    settlement_attempt = None
    if settlement_status is not None:
        settlement_attempt = SettlementCaptureAttempt(
            decision_snapshot_id=decision.id,
            benchmark_portfolio_id=portfolio.id,
            entry_capture_attempt_id=entry_attempt.id,
            status=settlement_status,
            underlying_price=(
                exit_underlying_price if settlement_status == CaptureStatus.CAPTURED else None
            ),
            realized_pnl=realized_pnl if settlement_status == CaptureStatus.CAPTURED else None,
            r_multiple=r_multiple if settlement_status == CaptureStatus.CAPTURED else None,
            is_win=is_win if settlement_status == CaptureStatus.CAPTURED else None,
            captured_at=settled_at if settlement_status == CaptureStatus.CAPTURED else None,
            capture_error=None if settlement_status == CaptureStatus.CAPTURED else "test failure",
        )
        db_session.add(settlement_attempt)
        db_session.flush()

    return decision, entry_attempt, settlement_attempt


# --------------------------------------------------------------------------
# "Never fabricate" -- zero settled trades
# --------------------------------------------------------------------------


def test_zero_decisions_returns_null_metrics_not_fake_zeros(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46A")

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.total_decisions == 0
    assert summary.settled_decisions == 0
    assert summary.win_rate.total == 0
    assert summary.win_rate.pct is None
    assert summary.average_r is None
    assert summary.median_r is None
    assert summary.expectancy is None
    assert summary.profit_factor is None
    assert summary.max_drawdown is None
    assert summary.max_drawdown_pct is None
    assert summary.directional_accuracy.pct is None
    assert summary.breakeven_accuracy.pct is None
    assert summary.range_accuracy.pct is None


def test_pending_and_failed_settlements_never_count_as_settled(db_session):
    """A decision with no settlement at all, and one with only a FAILED
    settlement attempt, must both be excluded from settled_decisions --
    never counted as a real data point."""
    portfolio = _seed_portfolio(db_session, "TESTTR46B")
    _seed_settled_decision(db_session, portfolio, "TESTTR46B1", settlement_status=None)
    _seed_settled_decision(
        db_session, portfolio, "TESTTR46B2", settlement_status=CaptureStatus.FAILED
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.total_decisions == 2
    assert summary.settled_decisions == 0
    assert summary.win_rate.pct is None


def test_calibration_zero_settled_returns_empty_buckets(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46C")

    summary = compute_benchmark_calibration(db_session, portfolio)

    assert summary.settled_decisions == 0
    assert len(summary.buckets) == 5
    assert all(bucket.rate.total == 0 for bucket in summary.buckets)
    assert all(bucket.rate.pct is None for bucket in summary.buckets)


# --------------------------------------------------------------------------
# Portfolio-level performance
# --------------------------------------------------------------------------


def test_single_win_computes_all_portfolio_metrics(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46D")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46D1",
        realized_pnl=Decimal("80.00"),
        r_multiple=Decimal("0.5"),
        is_win=True,
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.total_decisions == 1
    assert summary.settled_decisions == 1
    assert summary.win_rate.correct == 1
    assert summary.win_rate.total == 1
    assert summary.win_rate.pct == Decimal("1")
    assert summary.average_r == Decimal("0.5")
    assert summary.median_r == Decimal("0.5")
    assert summary.expectancy == Decimal("0.5")
    assert summary.profit_factor is None  # no losses yet
    assert summary.max_drawdown == Decimal("0")  # a single win never draws down
    assert summary.max_drawdown_pct == Decimal("0")


def test_win_rate_and_average_median_r_across_multiple_decisions(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46E")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46E1",
        realized_pnl=Decimal("100"),
        r_multiple=Decimal("1.0"),
        is_win=True,
        settled_at=datetime(2026, 9, 17, 15, 55, tzinfo=UTC),
    )
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46E2",
        realized_pnl=Decimal("-50"),
        r_multiple=Decimal("-0.5"),
        is_win=False,
        settled_at=datetime(2026, 9, 18, 15, 55, tzinfo=UTC),
    )
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46E3",
        realized_pnl=Decimal("60"),
        r_multiple=Decimal("0.6"),
        is_win=True,
        settled_at=datetime(2026, 9, 19, 15, 55, tzinfo=UTC),
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.settled_decisions == 3
    assert summary.win_rate.correct == 2
    assert summary.win_rate.total == 3
    assert summary.average_r == (Decimal("1.0") + Decimal("-0.5") + Decimal("0.6")) / 3
    assert summary.median_r == Decimal("0.6")
    # gross profit 160, gross loss 50 -> 3.2
    assert summary.profit_factor == Decimal("3.2")


def test_max_drawdown_uses_real_dollar_equity_curve_ordered_by_settlement(db_session):
    """Explicit regression for approved decision 1: ordered by
    captured_at (settlement time), based on real dollars against the
    $2,000 starting capital -- never R-multiples. Seeded out of
    chronological order to prove the service itself sorts by
    captured_at, not insertion order."""
    portfolio = _seed_portfolio(db_session, "TESTTR46F")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46F2",
        realized_pnl=Decimal("-300"),
        r_multiple=Decimal("-1.5"),
        is_win=False,
        settled_at=datetime(2026, 9, 18, 15, 55, tzinfo=UTC),
    )
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46F1",
        realized_pnl=Decimal("100"),
        r_multiple=Decimal("0.5"),
        is_win=True,
        settled_at=datetime(2026, 9, 17, 15, 55, tzinfo=UTC),
    )
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46F3",
        realized_pnl=Decimal("50"),
        r_multiple=Decimal("0.25"),
        is_win=True,
        settled_at=datetime(2026, 9, 19, 15, 55, tzinfo=UTC),
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    # chronological: 2000 -> 2100 (peak) -> 1800 (drawdown 300) -> 1850
    assert summary.max_drawdown == Decimal("300")
    assert summary.max_drawdown_pct == Decimal("300") / Decimal("2100") * 100


# --------------------------------------------------------------------------
# Prediction analytics
# --------------------------------------------------------------------------


def test_directional_accuracy_correct_bullish_call(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46G")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46G1",
        strategy_direction=DecisionDirection.BULLISH,
        entry_underlying_price=Decimal("100"),
        exit_underlying_price=Decimal("110"),
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.directional_accuracy.correct == 1
    assert summary.directional_accuracy.total == 1


def test_directional_accuracy_incorrect_when_move_is_opposite(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46H")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46H1",
        strategy_direction=DecisionDirection.BULLISH,
        entry_underlying_price=Decimal("100"),
        exit_underlying_price=Decimal("90"),
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.directional_accuracy.correct == 0
    assert summary.directional_accuracy.total == 1


def test_directional_accuracy_excludes_neutral_calls(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46I")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46I1",
        strategy_direction=DecisionDirection.NEUTRAL,
        entry_underlying_price=Decimal("100"),
        exit_underlying_price=Decimal("110"),
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.directional_accuracy.total == 0
    assert summary.directional_accuracy.pct is None


def test_breakeven_accuracy_long_call_cleared(db_session):
    """Long 100c bought at 2.10 -- breakeven 102.10. Exit underlying 110
    clears it -- correct."""
    portfolio = _seed_portfolio(db_session, "TESTTR46J")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46J1",
        entry_underlying_price=Decimal("100"),
        exit_underlying_price=Decimal("110"),
        leg_action=OptionAction.BUY,
        leg_strike=Decimal("100"),
        leg_entry_price=Decimal("2.10"),
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.breakeven_accuracy.correct == 1
    assert summary.breakeven_accuracy.total == 1


def test_breakeven_accuracy_long_call_not_cleared(db_session):
    """Exit underlying 101 does not clear the 102.10 breakeven -- incorrect."""
    portfolio = _seed_portfolio(db_session, "TESTTR46K")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46K1",
        entry_underlying_price=Decimal("100"),
        exit_underlying_price=Decimal("101"),
        leg_action=OptionAction.BUY,
        leg_strike=Decimal("100"),
        leg_entry_price=Decimal("2.10"),
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.breakeven_accuracy.correct == 0
    assert summary.breakeven_accuracy.total == 1


def test_range_accuracy_within_implied_move(db_session):
    """Implied move 8%, actual move 5% -- stayed within range -- correct."""
    portfolio = _seed_portfolio(db_session, "TESTTR46L")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46L1",
        entry_underlying_price=Decimal("100"),
        exit_underlying_price=Decimal("105"),
        implied_move_pct=Decimal("0.08"),
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.range_accuracy.correct == 1
    assert summary.range_accuracy.total == 1


def test_range_accuracy_exceeded_implied_move(db_session):
    """Implied move 3%, actual move 10% -- exceeded the range -- incorrect."""
    portfolio = _seed_portfolio(db_session, "TESTTR46M")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46M1",
        entry_underlying_price=Decimal("100"),
        exit_underlying_price=Decimal("110"),
        implied_move_pct=Decimal("0.03"),
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.range_accuracy.correct == 0
    assert summary.range_accuracy.total == 1


def test_range_accuracy_excluded_when_no_volatility_snapshot(db_session):
    """No option_snapshot_reference on record -- excluded, never guessed."""
    portfolio = _seed_portfolio(db_session, "TESTTR46N")
    _seed_settled_decision(
        db_session,
        portfolio,
        "TESTTR46N1",
        entry_underlying_price=Decimal("100"),
        exit_underlying_price=Decimal("105"),
        implied_move_pct=None,
    )

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.range_accuracy.total == 0
    assert summary.range_accuracy.pct is None


# --------------------------------------------------------------------------
# Probability calibration
# --------------------------------------------------------------------------


def test_calibration_buckets_a_decision_correctly(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46O")
    _seed_settled_decision(
        db_session, portfolio, "TESTTR46O1", estimated_probability=Decimal("0.72"), is_win=True
    )
    _seed_settled_decision(
        db_session, portfolio, "TESTTR46O2", estimated_probability=Decimal("0.75"), is_win=False
    )
    _seed_settled_decision(
        db_session, portfolio, "TESTTR46O3", estimated_probability=Decimal("0.30"), is_win=True
    )

    summary = compute_benchmark_calibration(db_session, portfolio)

    assert summary.settled_decisions == 3
    by_label = {b.label: b for b in summary.buckets}
    assert by_label["70-80%"].rate.correct == 1
    assert by_label["70-80%"].rate.total == 2
    assert by_label["<60%"].rate.correct == 1
    assert by_label["<60%"].rate.total == 1
    assert by_label["60-70%"].rate.total == 0
    assert by_label["80-90%"].rate.total == 0
    assert by_label["90%+"].rate.total == 0


def test_calibration_excludes_decisions_with_no_probability_estimate(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46P")
    _seed_settled_decision(db_session, portfolio, "TESTTR46P1", estimated_probability=None)

    summary = compute_benchmark_calibration(db_session, portfolio)

    assert summary.settled_decisions == 1  # a real settled decision...
    assert all(bucket.rate.total == 0 for bucket in summary.buckets)  # ...just ungraded


# --------------------------------------------------------------------------
# Filters (strategy breakdown)
# --------------------------------------------------------------------------


def test_strategy_filter_narrows_the_decision_set(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46Q")
    _seed_settled_decision(db_session, portfolio, "TESTTR46Q1", strategy_type="long_call")
    _seed_settled_decision(db_session, portfolio, "TESTTR46Q2", strategy_type="iron_condor")

    summary = compute_benchmark_track_record(
        db_session, portfolio, TrackRecordFilters(strategy="iron_condor")
    )

    assert summary.total_decisions == 1
    assert summary.settled_decisions == 1


def test_dte_bucket_filter(db_session):
    """EXP is 2 days after EARNINGS_DATE -> "0-3" bucket."""
    portfolio = _seed_portfolio(db_session, "TESTTR46R")
    _seed_settled_decision(db_session, portfolio, "TESTTR46R1", selected_expiration=EXP)

    matching = compute_benchmark_track_record(
        db_session, portfolio, TrackRecordFilters(dte_bucket="0-3")
    )
    non_matching = compute_benchmark_track_record(
        db_session, portfolio, TrackRecordFilters(dte_bucket="30+")
    )

    assert matching.total_decisions == 1
    assert non_matching.total_decisions == 0


def test_confidence_bucket_filter(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46S")
    _seed_settled_decision(
        db_session, portfolio, "TESTTR46S1", estimated_probability=Decimal("0.85")
    )

    matching = compute_benchmark_track_record(
        db_session, portfolio, TrackRecordFilters(confidence_bucket="80-90%")
    )
    non_matching = compute_benchmark_track_record(
        db_session, portfolio, TrackRecordFilters(confidence_bucket="<60%")
    )

    assert matching.total_decisions == 1
    assert non_matching.total_decisions == 0


def test_iv_regime_filter(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46T")
    _seed_settled_decision(db_session, portfolio, "TESTTR46T1", volatility_regime="high")

    matching = compute_benchmark_track_record(
        db_session, portfolio, TrackRecordFilters(iv_regime="high")
    )
    non_matching = compute_benchmark_track_record(
        db_session, portfolio, TrackRecordFilters(iv_regime="low")
    )

    assert matching.total_decisions == 1
    assert non_matching.total_decisions == 0


def test_risk_profile_filter_matches_the_portfolios_own_profile(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46U")  # MODERATE
    _seed_settled_decision(db_session, portfolio, "TESTTR46U1")

    matching = compute_benchmark_track_record(
        db_session, portfolio, TrackRecordFilters(risk_profile=RiskProfile.MODERATE)
    )
    non_matching = compute_benchmark_track_record(
        db_session, portfolio, TrackRecordFilters(risk_profile=RiskProfile.AGGRESSIVE)
    )

    assert matching.total_decisions == 1
    assert non_matching.total_decisions == 0


def test_filters_combine_with_and_semantics(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46V")
    _seed_settled_decision(
        db_session, portfolio, "TESTTR46V1", strategy_type="iron_condor", volatility_regime="high"
    )
    _seed_settled_decision(
        db_session, portfolio, "TESTTR46V2", strategy_type="iron_condor", volatility_regime="low"
    )

    summary = compute_benchmark_track_record(
        db_session, portfolio, TrackRecordFilters(strategy="iron_condor", iv_regime="high")
    )

    assert summary.total_decisions == 1


# --------------------------------------------------------------------------
# resolve_portfolio
# --------------------------------------------------------------------------


def test_resolve_portfolio_defaults_to_the_active_one(db_session):
    """The shared dev DB carries a real, already-seeded active portfolio
    (Phase 4.4's own migration seed, id=1) -- deactivated here, inside
    this test's own rolled-back transaction only, so the resolution is
    deterministic without depending on this test running against a
    pristine table (matching this project's own established pattern for
    tests that need a table's real contents temporarily out of the way;
    see tests/conftest.py::clean_provider_state's docstring for the same
    reasoning applied to other tables)."""
    db_session.query(BenchmarkPortfolio).filter(BenchmarkPortfolio.is_active.is_(True)).update(
        {"is_active": False}
    )
    portfolio = _seed_portfolio(db_session, "TESTTR46W")

    resolved = resolve_portfolio(db_session, None)

    assert resolved is not None
    assert resolved.id == portfolio.id


def test_resolve_portfolio_by_explicit_id(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46X")

    resolved = resolve_portfolio(db_session, portfolio.id)

    assert resolved is not None
    assert resolved.id == portfolio.id


def test_resolve_portfolio_returns_none_for_unknown_id(db_session):
    assert resolve_portfolio(db_session, 999_999_999) is None


# --------------------------------------------------------------------------
# Post-live correction (2026-08-25) -- pre-settlement action summary.
# Real Aug 25 shape: 8 real decisions, 1 real captured entry, 0 settled.
# _seed_settled_decision above never sets decision_snapshot.legs (only
# EntrySnapshot rows, a separate table) -- these tests construct the
# real, minimal shapes directly instead, matching what services/
# decision_snapshot_freezing.py actually freezes.
# --------------------------------------------------------------------------


def _seed_event(db_session, symbol: str) -> EarningsCalendarEvent:
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Co",
        earnings_date=EARNINGS_DATE,
        earnings_time=EarningsTiming.AMC,
    )
    db_session.add(event)
    db_session.flush()
    return event


def _seed_decision(
    db_session,
    portfolio: BenchmarkPortfolio,
    symbol: str,
    *,
    legs: list[dict] | None,
) -> DecisionSnapshot:
    event = _seed_event(db_session, symbol)
    decision = DecisionSnapshot(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=symbol,
        company_name=event.company_name,
        strategy_direction=DecisionDirection.NEUTRAL,
        strategy_type="long_call_butterfly" if legs else None,
        generated_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
        status=DecisionSnapshotStatus.PENDING_ENTRY,
        legs=legs,
        engine_version="options-decision-engine-v3",
        prompt_version="v1",
        expiration_source="v3_auto_resolver",
    )
    db_session.add(decision)
    db_session.flush()
    return decision


_A_LEG = [
    {
        "option_type": "call",
        "action": "buy",
        "strike": "100.00",
        "premium": "5.00",
        "quantity": 1,
    }
]


def test_action_summary_splits_actionable_from_no_action_decisions(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46AS1")
    _seed_decision(db_session, portfolio, "TESTASACT1", legs=_A_LEG)
    _seed_decision(db_session, portfolio, "TESTASACT2", legs=_A_LEG)
    _seed_decision(db_session, portfolio, "TESTASNOACT", legs=None)

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.total_decisions == 3
    assert summary.actionable_decisions == 2
    assert summary.no_action_decisions == 1


def test_a_no_action_decisions_failed_attempt_never_counts_as_entry_capture_failed(db_session):
    """The real Aug 25 SJM shape: a no-action decision still gets a real
    FAILED EntryCaptureAttempt ("no recommended strategy legs to enter",
    see services/benchmark_entry_capture.py), but that must never be
    counted the same as a genuine infrastructure entry-capture failure."""
    portfolio = _seed_portfolio(db_session, "TESTTR46AS2")
    no_action = _seed_decision(db_session, portfolio, "TESTASNOACT2", legs=None)
    db_session.add(
        EntryCaptureAttempt(
            decision_snapshot_id=no_action.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.FAILED,
            capture_error="decision_snapshot has no recommended strategy legs to enter",
        )
    )
    db_session.flush()

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.no_action_decisions == 1
    assert summary.entries_captured == 0
    assert summary.entries_capture_failed == 0


def test_entries_captured_and_capture_failed_count_only_actionable_decisions(db_session):
    portfolio = _seed_portfolio(db_session, "TESTTR46AS3")
    captured = _seed_decision(db_session, portfolio, "TESTASCAP", legs=_A_LEG)
    db_session.add(
        EntryCaptureAttempt(
            decision_snapshot_id=captured.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.CAPTURED,
            captured_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
        )
    )
    failed = _seed_decision(db_session, portfolio, "TESTASFAIL", legs=_A_LEG)
    db_session.add(
        EntryCaptureAttempt(
            decision_snapshot_id=failed.id,
            benchmark_portfolio_id=portfolio.id,
            status=CaptureStatus.FAILED,
            capture_error="no ask quote available for a long leg",
        )
    )
    db_session.flush()

    summary = compute_benchmark_track_record(db_session, portfolio, TrackRecordFilters())

    assert summary.actionable_decisions == 2
    assert summary.entries_captured == 1
    assert summary.entries_capture_failed == 1
