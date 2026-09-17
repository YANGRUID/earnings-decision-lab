"""Operator verification of an earnings date (2026-09-17 audit: GIS on the
wrong date, a phantom FERG report, ACN recorded as after the close)."""

from datetime import UTC, date, datetime

import pytest

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsTiming
from services.calendar_verification import (
    CalendarVerificationError,
    apply_calendar_verification,
)


def _event(
    db, symbol, day, timing=EarningsTiming.UNKNOWN, status=EarningsCalendarEventStatus.UPCOMING
):
    row = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Inc",
        earnings_date=day,
        earnings_time=timing,
        status=status,
    )
    db.add(row)
    db.flush()
    return row


def test_a_wrong_date_is_moved_and_pinned(db_session):
    row = _event(db_session, "VGISX", date(2032, 9, 15), status=EarningsCalendarEventStatus.SKIPPED)

    result = apply_calendar_verification(
        db_session,
        row,
        earnings_date=date(2032, 9, 23),
        timing=EarningsTiming.BMO,
        note="issuer press release",
        now=datetime(2032, 9, 17, tzinfo=UTC),
    )

    assert row.earnings_date == date(2032, 9, 23)
    assert row.earnings_time == EarningsTiming.BMO
    assert row.status == EarningsCalendarEventStatus.UPCOMING
    assert row.verified_at is not None and row.verification_note == "issuer press release"
    assert result.before["earnings_date"] == "2032-09-15"
    assert result.after["earnings_date"] == "2032-09-23"


def test_a_phantom_report_becomes_a_verified_non_event(db_session):
    row = _event(db_session, "VFERGX", date(2032, 9, 22))
    apply_calendar_verification(db_session, row, no_report=True, note="calendar-quarter reporter")
    assert row.status == EarningsCalendarEventStatus.SKIPPED
    assert row.verified_at is not None


def test_a_source_is_required(db_session):
    row = _event(db_session, "VNOSRC", date(2032, 9, 22))
    with pytest.raises(CalendarVerificationError):
        apply_calendar_verification(db_session, row, no_report=True, note="  ")


def test_an_event_with_a_frozen_decision_is_never_rewritten(db_session):
    from models.v4_shadow import V4ShadowDecision

    row = _event(db_session, "VDECX", date(2032, 9, 10), EarningsTiming.AMC)
    stamp = datetime(2032, 9, 10, 19, 30, tzinfo=UTC)
    db_session.add(
        V4ShadowDecision(
            earnings_calendar_event_id=row.id,
            ticker="VDECX",
            company_name="Dec Co",
            legal_decision_window_at=stamp,
            generated_at=stamp,
            as_of=stamp,
            status="RANKED",
            engine_version="v4-test",
            shadow_schema_version="test",
            candidate_count=0,
            rankable_candidate_count=0,
        )
    )
    db_session.flush()

    with pytest.raises(CalendarVerificationError):
        apply_calendar_verification(
            db_session, row, earnings_date=date(2032, 9, 12), note="press release"
        )
    assert row.earnings_date == date(2032, 9, 10)


def test_a_move_onto_an_existing_event_is_refused(db_session):
    row = _event(db_session, "VCLSH", date(2032, 9, 15))
    _event(db_session, "VCLSH", date(2032, 9, 23))
    with pytest.raises(CalendarVerificationError):
        apply_calendar_verification(
            db_session, row, earnings_date=date(2032, 9, 23), note="press release"
        )


def test_a_missing_issuer_announced_report_is_created_verified(db_session):
    from decimal import Decimal

    from services.calendar_verification import create_verified_event

    result = create_verified_event(
        db_session,
        symbol="vcclx",
        earnings_date=date(2032, 9, 29),
        timing=EarningsTiming.BMO,
        note="issuer press release",
        company_name="VCCLX Corp",
        market_cap=Decimal("35000000000"),
        country="US",
    )
    row = db_session.get(EarningsCalendarEvent, result.event_id)
    assert row.symbol == "VCCLX" and row.earnings_time == EarningsTiming.BMO
    assert row.status == EarningsCalendarEventStatus.UPCOMING
    assert row.verified_at is not None and row.profile_refreshed_at is not None

    with pytest.raises(CalendarVerificationError):
        create_verified_event(
            db_session,
            symbol="VCCLX",
            earnings_date=date(2032, 9, 29),
            timing=EarningsTiming.BMO,
            note="again",
            company_name="VCCLX Corp",
            market_cap=None,
            country="US",
        )
