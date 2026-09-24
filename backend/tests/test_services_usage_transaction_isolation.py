"""Recording telemetry must never commit or discard the caller's own work.

Measured defect (2026-09-18 .. 09-21). record_usage_event added its row to the
CALLER's session and committed it. A commit flushes everything pending in that
session, so a provider call made while building something else committed that
work early -- and when the flush failed, the caller's rows were rolled back and
the failure was logged as "failed to record provider usage event". The nightly
earnings calendar sync hit exactly that: one un-storable provider estimate
surfaced as a swallowed usage warning while the sync reported success.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsSource, EarningsTiming
from models.provider_usage_event import ProviderUsageEvent
from services.usage_instrumentation import record_usage_event


def _pending_event(db) -> EarningsCalendarEvent:
    row = EarningsCalendarEvent(
        symbol="TESTPEND",
        company_name="Pending Inc",
        earnings_date=date(2030, 1, 5),
        earnings_time=EarningsTiming.BMO,
        status=EarningsCalendarEventStatus.UPCOMING,
        source=EarningsSource.EARNINGSAPI,
        market_cap=Decimal("50000000000"),
        country="US",
    )
    db.add(row)
    return row


def test_recording_usage_does_not_commit_the_callers_pending_work(db_session):
    """The caller decides when its own work becomes durable."""
    _pending_event(db_session)

    record_usage_event(
        db_session,
        provider="earningsapi",
        domain="earnings_calendar",
        operation="get_earnings_calendar",
        success=True,
        latency_ms=5,
    )

    # Still the caller's to commit or discard: never flushed by the telemetry.
    assert any(isinstance(obj, EarningsCalendarEvent) for obj in db_session.new)


def test_the_usage_row_is_written_even_so(db_session):
    record_usage_event(
        db_session,
        provider="earningsapi",
        domain="earnings_calendar",
        operation="get_earnings_calendar",
        success=True,
        latency_ms=5,
        provider_units=3,
    )

    written = (
        db_session.query(ProviderUsageEvent)
        .filter_by(provider="earningsapi", operation="get_earnings_calendar")
        .order_by(ProviderUsageEvent.id.desc())
        .first()
    )
    assert written is not None and written.provider_units == 3


def test_a_recording_failure_never_raises_into_the_caller(db_session, monkeypatch):
    """Telemetry is observability: a DB hiccup must not break the real call."""

    def _explode(*_args, **_kwargs):
        raise RuntimeError("no connection")

    monkeypatch.setattr(db_session, "get_bind", _explode)
    record_usage_event(
        db_session,
        provider="earningsapi",
        domain="earnings_calendar",
        operation="get_earnings_calendar",
        success=True,
        latency_ms=5,
    )


def test_an_unrelated_caller_failure_does_not_erase_recorded_usage(db_session):
    """The other direction: a business rollback must not take telemetry with
    it, or a spent provider request would vanish from the usage record."""
    record_usage_event(
        db_session,
        provider="earningsapi",
        domain="earnings_calendar",
        operation="get_company_profile",
        success=False,
        latency_ms=5,
        status_code="FREE_QUOTA_EXCEEDED",
        rate_limited=True,
    )
    _pending_event(db_session)
    db_session.rollback()

    assert (
        db_session.query(ProviderUsageEvent)
        .filter_by(status_code="FREE_QUOTA_EXCEEDED", operation="get_company_profile")
        .count()
        >= 1
    )
