from datetime import UTC, datetime

from analytics.market_session import EASTERN, MarketSession, get_market_session


def _utc_from_eastern(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN).astimezone(UTC)


class TestGetMarketSession:
    def test_pre_market_window(self):
        # Wednesday 2026-03-18, 07:00 ET -- inside 04:00-09:30 pre-market.
        status = get_market_session(_utc_from_eastern(2026, 3, 18, 7, 0))
        assert status.session == MarketSession.PRE_MARKET
        assert status.opens_in_minutes is None

    def test_regular_session_window(self):
        status = get_market_session(_utc_from_eastern(2026, 3, 18, 11, 0))
        assert status.session == MarketSession.REGULAR
        assert status.opens_in_minutes is None

    def test_after_hours_window(self):
        status = get_market_session(_utc_from_eastern(2026, 3, 18, 17, 0))
        assert status.session == MarketSession.AFTER_HOURS
        assert status.opens_in_minutes is None

    def test_closed_late_night_reports_minutes_to_next_open(self):
        # Wednesday 23:00 ET -- closed, next regular open is Thursday 09:30 ET.
        status = get_market_session(_utc_from_eastern(2026, 3, 18, 23, 0))
        assert status.session == MarketSession.CLOSED
        assert status.opens_in_minutes == 630  # 10h30m to 09:30 the next day

    def test_weekend_is_closed_regardless_of_time_of_day(self):
        # 2026-03-21 is a Saturday.
        status = get_market_session(_utc_from_eastern(2026, 3, 21, 11, 0))
        assert status.session == MarketSession.CLOSED
        assert status.opens_in_minutes is not None

    def test_weekend_next_open_skips_to_monday(self):
        # Saturday 2026-03-21 10:00 ET -- next regular open must be Monday
        # 2026-03-23 09:30 ET, not Sunday: 2 days minus 30 minutes.
        status = get_market_session(_utc_from_eastern(2026, 3, 21, 10, 0))
        assert status.session == MarketSession.CLOSED
        assert status.opens_in_minutes == 2 * 24 * 60 - 30

    def test_defaults_to_real_current_time_when_as_of_omitted(self):
        status = get_market_session()
        assert status.session in set(MarketSession)

    def test_boundary_exactly_at_regular_open_is_regular(self):
        status = get_market_session(_utc_from_eastern(2026, 3, 18, 9, 30))
        assert status.session == MarketSession.REGULAR

    def test_boundary_exactly_at_regular_close_is_after_hours(self):
        status = get_market_session(_utc_from_eastern(2026, 3, 18, 16, 0))
        assert status.session == MarketSession.AFTER_HOURS
