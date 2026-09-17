"""One earnings report listed under several share classes (LEN / LEN.B,
2026-09-16) is one observation, never two."""

from datetime import date

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsTiming
from services.listing_identity import duplicate_listing_of, share_class_root


def _event(db, symbol, day=date(2031, 3, 5)):
    row = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Corp",
        earnings_date=day,
        earnings_time=EarningsTiming.AMC,
        status=EarningsCalendarEventStatus.UPCOMING,
    )
    db.add(row)
    db.flush()
    return row


def test_share_class_roots():
    assert share_class_root("LEN.B") == "LEN"
    assert share_class_root("brk-b") == "BRK"
    assert share_class_root("LEN") is None
    assert share_class_root("OAK PR A") is None
    assert share_class_root("A.B.C") is None


def test_a_class_listing_duplicates_the_same_days_plain_listing(db_session):
    plain = _event(db_session, "ZZLEN")
    klass = _event(db_session, "ZZLEN.B")
    assert duplicate_listing_of(db_session, klass).id == plain.id
    assert duplicate_listing_of(db_session, plain) is None


def test_a_class_listing_on_another_day_is_its_own_event(db_session):
    _event(db_session, "ZZLNX", date(2031, 3, 5))
    other = _event(db_session, "ZZLNX.B", date(2031, 6, 5))
    assert duplicate_listing_of(db_session, other) is None


def test_two_suffixed_classes_without_a_plain_listing_are_not_guessed_between(db_session):
    a = _event(db_session, "ZZBF.A")
    b = _event(db_session, "ZZBF.B")
    assert duplicate_listing_of(db_session, a) is None
    assert duplicate_listing_of(db_session, b) is None
