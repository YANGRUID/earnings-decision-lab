"""Phase 4 quote-observability hardening (2026-08-26), Sections 13-14 --
unit tests for services/quote_diagnostics.py, the Operations read model
over real QuoteAcquisitionAttempt rows. Rows are constructed directly
(not via the full capture_benchmark_entry flow, already covered by
tests/test_services_benchmark_entry_capture.py) -- this module tests the
read/aggregation logic in isolation.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from models.benchmark_portfolio import BenchmarkPortfolio
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.entry_capture_attempt import EntryCaptureAttempt
from models.enums import (
    CaptureStatus,
    DecisionDirection,
    EarningsTiming,
    OptionType,
    QuoteAcquisitionCaptureType,
    QuoteRequirement,
)
from models.quote_acquisition_attempt import QuoteAcquisitionAttempt
from services.quote_diagnostics import (
    TELEMETRY_FEATURE_ACTIVATED_AT,
    detect_missing_quote_telemetry,
    get_entry_quote_diagnostics,
    get_quote_diagnostics_summary,
)

EXP = date(2026, 9, 18)


def _seed_entry_attempt(
    db_session,
    ticker: str = "ZZDIAG",
    *,
    status: CaptureStatus = CaptureStatus.FAILED,
    capture_error: str | None = "no ask quote available for a long leg",
    created_at: datetime | None = None,
) -> EntryCaptureAttempt:
    event = EarningsCalendarEvent(
        symbol=ticker,
        company_name="ZZ Diagnostics Co",
        earnings_date=date(2026, 9, 17),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name=f"Diag Portfolio {ticker}",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
    )
    db_session.add_all([event, portfolio])
    db_session.flush()

    decision = DecisionSnapshot(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker=ticker,
        company_name="ZZ Diagnostics Co",
        strategy_direction=DecisionDirection.BULLISH,
        generated_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
        engine_version="test",
        prompt_version="test",
        expiration_source="test",
    )
    db_session.add(decision)
    db_session.flush()

    attempt = EntryCaptureAttempt(
        decision_snapshot_id=decision.id,
        benchmark_portfolio_id=portfolio.id,
        status=status,
        capture_error=capture_error,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    db_session.add(attempt)
    db_session.flush()
    return attempt


def _row(attempt: EntryCaptureAttempt, ticker: str, **overrides) -> QuoteAcquisitionAttempt:
    defaults = dict(
        capture_attempt_type=QuoteAcquisitionCaptureType.ENTRY,
        entry_capture_attempt_id=attempt.id,
        ticker=ticker,
        leg_index=0,
        expiration=EXP,
        option_type=OptionType.CALL,
        strike=Decimal("100"),
        required_side=QuoteRequirement.ASK,
        snapshot_attempt_number=1,
        observed_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
        elapsed_ms=1500,
        bid_present=True,
        ask_present=False,
        last_present=True,
        bid=Decimal("1.90"),
        ask=None,
        last_price=Decimal("2.00"),
        market_data_quality="delayed",
        contract_resolved=True,
        final_for_leg=True,
    )
    defaults.update(overrides)
    return QuoteAcquisitionAttempt(**defaults)


class TestGetEntryQuoteDiagnostics:
    def test_none_when_no_telemetry_exists(self, db_session):
        attempt = _seed_entry_attempt(db_session)
        assert get_entry_quote_diagnostics(db_session, attempt.id) is None

    def test_groups_multiple_attempts_by_leg_in_order(self, db_session):
        attempt = _seed_entry_attempt(db_session)
        db_session.add_all(
            [
                _row(
                    attempt,
                    "ZZDIAG",
                    snapshot_attempt_number=1,
                    elapsed_ms=0,
                    ask_present=False,
                    bid_present=False,
                    last_present=True,
                    final_for_leg=False,
                ),
                _row(
                    attempt,
                    "ZZDIAG",
                    snapshot_attempt_number=2,
                    elapsed_ms=1510,
                    ask_present=False,
                    bid_present=True,
                    last_present=True,
                    final_for_leg=False,
                ),
                _row(
                    attempt,
                    "ZZDIAG",
                    snapshot_attempt_number=3,
                    elapsed_ms=3024,
                    ask_present=True,
                    ask=Decimal("2.10"),
                    bid_present=True,
                    last_present=True,
                    final_for_leg=True,
                ),
            ]
        )
        db_session.flush()

        diagnostics = get_entry_quote_diagnostics(db_session, attempt.id)
        assert diagnostics is not None
        assert diagnostics.ticker == "ZZDIAG"
        assert diagnostics.expiration == EXP.isoformat()
        assert len(diagnostics.legs) == 1
        leg = diagnostics.legs[0]
        assert leg.leg_index == 0
        assert leg.option_type == "call"
        assert leg.strike == Decimal("100")
        assert leg.required_side == "ask"
        assert leg.contract_resolved is True
        assert len(leg.attempts) == 3
        assert [a.snapshot_attempt_number for a in leg.attempts] == [1, 2, 3]
        assert "ASK acquired after 3.024s" == leg.result_label

    def test_result_label_for_never_acquired_side(self, db_session):
        attempt = _seed_entry_attempt(db_session)
        db_session.add(_row(attempt, "ZZDIAG", ask_present=False, final_for_leg=True))
        db_session.flush()

        diagnostics = get_entry_quote_diagnostics(db_session, attempt.id)
        assert diagnostics is not None
        assert diagnostics.legs[0].result_label == "ASK unavailable after bounded retry"

    def test_result_label_for_rate_limited(self, db_session):
        attempt = _seed_entry_attempt(db_session)
        db_session.add(
            _row(
                attempt,
                "ZZDIAG",
                snapshot_attempt_number=0,
                elapsed_ms=0,
                bid_present=False,
                ask_present=False,
                last_present=False,
                rate_limited=True,
                provider_error_category="RATE_LIMITED",
            )
        )
        db_session.flush()

        diagnostics = get_entry_quote_diagnostics(db_session, attempt.id)
        assert diagnostics is not None
        assert diagnostics.legs[0].result_label == "RATE LIMITED"

    def test_result_label_for_contract_unavailable(self, db_session):
        attempt = _seed_entry_attempt(db_session)
        db_session.add(
            _row(
                attempt,
                "ZZDIAG",
                snapshot_attempt_number=0,
                elapsed_ms=0,
                bid_present=False,
                ask_present=False,
                last_present=False,
                contract_resolved=False,
            )
        )
        db_session.flush()

        diagnostics = get_entry_quote_diagnostics(db_session, attempt.id)
        assert diagnostics is not None
        assert diagnostics.legs[0].result_label == "CONTRACT UNAVAILABLE"

    def test_multi_leg_each_leg_kept_separate(self, db_session):
        attempt = _seed_entry_attempt(db_session)
        db_session.add_all(
            [
                _row(attempt, "ZZDIAG", leg_index=0, strike=Decimal("100")),
                _row(
                    attempt,
                    "ZZDIAG",
                    leg_index=1,
                    strike=Decimal("105"),
                    required_side=QuoteRequirement.BID,
                    ask_present=True,
                    bid_present=False,
                ),
            ]
        )
        db_session.flush()

        diagnostics = get_entry_quote_diagnostics(db_session, attempt.id)
        assert diagnostics is not None
        assert len(diagnostics.legs) == 2
        assert [leg.leg_index for leg in diagnostics.legs] == [0, 1]

    def test_never_exposes_a_secret_field(self, db_session):
        """Section 13 -- no account id, username, session id, cookie,
        auth token, or password anywhere in the diagnostic dataclasses."""
        attempt = _seed_entry_attempt(db_session)
        db_session.add(_row(attempt, "ZZDIAG"))
        db_session.flush()

        diagnostics = get_entry_quote_diagnostics(db_session, attempt.id)
        assert diagnostics is not None
        from dataclasses import asdict

        blob = str(asdict(diagnostics.legs[0])).lower()
        for banned in ("account", "username", "session_id", "cookie", "token", "password"):
            assert banned not in blob


class TestGetQuoteDiagnosticsSummary:
    def test_empty_window_returns_zeros(self, db_session):
        summary = get_quote_diagnostics_summary(db_session, now=datetime(2099, 1, 1, tzinfo=UTC))
        assert summary.contracts_requested == 0
        assert summary.contracts_resolved == 0
        assert summary.total_snapshot_attempts == 0
        assert summary.average_attempts_per_leg is None
        assert summary.median_attempts_per_leg is None

    def test_counts_resolved_and_unresolved_final_legs(self, db_session):
        attempt = _seed_entry_attempt(db_session)
        now = datetime(2026, 9, 16, 16, 0, tzinfo=UTC)
        db_session.add_all(
            [
                _row(
                    attempt,
                    "ZZDIAG",
                    leg_index=0,
                    observed_at=now,
                    snapshot_attempt_number=1,
                    contract_resolved=True,
                    final_for_leg=True,
                    ask_present=True,
                ),
                _row(
                    attempt,
                    "ZZDIAG",
                    leg_index=1,
                    observed_at=now,
                    snapshot_attempt_number=0,
                    contract_resolved=False,
                    final_for_leg=True,
                    bid_present=False,
                    ask_present=False,
                    last_present=False,
                ),
            ]
        )
        db_session.flush()

        summary = get_quote_diagnostics_summary(
            db_session, now=datetime(2026, 9, 16, 17, 0, tzinfo=UTC)
        )
        assert summary.contracts_requested == 2
        assert summary.contracts_resolved == 1
        assert summary.contract_error_count == 1

    def test_rate_limited_and_permission_error_counted_separately(self, db_session):
        attempt = _seed_entry_attempt(db_session)
        now = datetime(2026, 9, 16, 16, 0, tzinfo=UTC)
        db_session.add_all(
            [
                _row(
                    attempt,
                    "ZZDIAG",
                    leg_index=0,
                    observed_at=now,
                    snapshot_attempt_number=0,
                    rate_limited=True,
                    bid_present=False,
                    ask_present=False,
                    last_present=False,
                ),
                _row(
                    attempt,
                    "ZZDIAG",
                    leg_index=1,
                    observed_at=now,
                    snapshot_attempt_number=0,
                    permission_error=True,
                    bid_present=False,
                    ask_present=False,
                    last_present=False,
                ),
            ]
        )
        db_session.flush()

        summary = get_quote_diagnostics_summary(
            db_session, now=datetime(2026, 9, 16, 17, 0, tzinfo=UTC)
        )
        assert summary.rate_limited_count == 1
        assert summary.permission_error_count == 1

    def test_rows_outside_the_window_excluded(self, db_session):
        attempt = _seed_entry_attempt(db_session)
        stale = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)  # well outside a 48h window
        db_session.add(_row(attempt, "ZZDIAG", observed_at=stale))
        db_session.flush()

        summary = get_quote_diagnostics_summary(
            db_session, now=datetime(2026, 9, 16, 17, 0, tzinfo=UTC), window_hours=48
        )
        assert summary.contracts_requested == 0


class TestDetectMissingQuoteTelemetry:
    """Phase 4 quote-observability hardening (2026-08-26), Section 19 --
    validates the telemetry wiring prospectively: a real capture that
    reached the provider call but left zero QuoteAcquisitionAttempt rows
    must be flagged, while every legitimate "never reached the provider"
    early-return failure must never be."""

    def test_captured_with_no_telemetry_is_flagged(self, db_session):
        attempt = _seed_entry_attempt(
            db_session, "ZZMISS1", status=CaptureStatus.CAPTURED, capture_error=None
        )
        db_session.flush()

        alerts = detect_missing_quote_telemetry(db_session)

        matching = [a for a in alerts if a.entry_capture_attempt_id == attempt.id]
        assert len(matching) == 1
        assert matching[0].capture_attempt_type == "entry"
        assert matching[0].ticker == "ZZMISS1"

    def test_provider_call_failure_with_no_telemetry_is_flagged(self, db_session):
        attempt = _seed_entry_attempt(
            db_session,
            "ZZMISS2",
            status=CaptureStatus.FAILED,
            capture_error="options provider call failed: IBKR Client Portal Gateway rate-limited",
        )
        db_session.flush()

        alerts = detect_missing_quote_telemetry(db_session)

        assert any(a.entry_capture_attempt_id == attempt.id for a in alerts)

    def test_provider_call_failure_with_real_telemetry_is_not_flagged(self, db_session):
        attempt = _seed_entry_attempt(
            db_session,
            "ZZMISS3",
            status=CaptureStatus.FAILED,
            capture_error="options provider call failed: IBKR Client Portal Gateway rate-limited",
        )
        db_session.add(
            _row(attempt, "ZZMISS3", rate_limited=True, provider_error_category="RATE_LIMITED")
        )
        db_session.flush()

        alerts = detect_missing_quote_telemetry(db_session)

        assert not any(a.entry_capture_attempt_id == attempt.id for a in alerts)

    def test_early_return_validation_failure_never_flagged(self, db_session):
        """This is the correct, expected shape for a real early-return
        failure -- the provider was never called, so no telemetry is
        exactly right, never a wiring gap."""
        attempt = _seed_entry_attempt(
            db_session,
            "ZZMISS4",
            status=CaptureStatus.FAILED,
            capture_error="decision_snapshot has no recommended strategy legs to enter",
        )
        db_session.flush()

        alerts = detect_missing_quote_telemetry(db_session)

        assert not any(a.entry_capture_attempt_id == attempt.id for a in alerts)

    def test_legacy_pre_telemetry_capture_never_flagged(self, db_session):
        """Post-official-run validation (2026-08-27), Section 20 -- a real
        CAPTURED entry from before TELEMETRY_FEATURE_ACTIVATED_AT (the
        quote_acquisition_attempt table's own migration deploy instant)
        can never have telemetry without violating this project's
        immutability guarantee. It must never appear as an active
        Operations alert, even while still inside the 72h rolling
        lookback relative to ``now``."""
        legacy_created_at = TELEMETRY_FEATURE_ACTIVATED_AT - datetime.resolution
        attempt = _seed_entry_attempt(
            db_session,
            "ZZLEGACY",
            status=CaptureStatus.CAPTURED,
            capture_error=None,
            created_at=legacy_created_at,
        )
        db_session.flush()

        alerts = detect_missing_quote_telemetry(
            db_session, now=legacy_created_at + timedelta(hours=1)
        )

        assert not any(a.entry_capture_attempt_id == attempt.id for a in alerts)

    def test_capture_right_after_activation_boundary_still_flagged(self, db_session):
        """The fix must not overreach: a genuine post-activation capture
        with zero telemetry is still a real wiring gap and must still be
        flagged."""
        just_after = TELEMETRY_FEATURE_ACTIVATED_AT + timedelta(seconds=1)
        attempt = _seed_entry_attempt(
            db_session,
            "ZZPOSTACT",
            status=CaptureStatus.CAPTURED,
            capture_error=None,
            created_at=just_after,
        )
        db_session.flush()

        alerts = detect_missing_quote_telemetry(db_session, now=just_after + timedelta(hours=1))

        assert any(a.entry_capture_attempt_id == attempt.id for a in alerts)
