from datetime import date, datetime, time

from analytics.earnings_timing import (
    ENTRY_EXIT_TIME,
    compute_entry_exit_schedule,
    is_trading_day,
    next_trading_day,
    previous_trading_day,
    us_market_holidays,
)
from analytics.market_session import EASTERN
from models.enums import AnnouncementTime


class TestUsMarketHolidays:
    def test_returns_ten_real_holidays_for_a_normal_year(self):
        holidays = us_market_holidays(2026)
        assert len(holidays) == 10

    def test_fixed_date_holiday_falling_on_saturday_shifts_to_preceding_friday(self):
        # 2026-07-04 (Independence Day) is a real Saturday.
        holidays = us_market_holidays(2026)
        assert date(2026, 7, 4) not in holidays  # weekend, and shifted away
        assert date(2026, 7, 3) in holidays

    def test_good_friday_computed_via_easter(self):
        assert date(2026, 4, 3) in us_market_holidays(2026)

    def test_nth_weekday_holidays_present(self):
        holidays = us_market_holidays(2026)
        assert date(2026, 1, 19) in holidays  # MLK Day, 3rd Monday of January
        assert date(2026, 2, 16) in holidays  # Presidents Day, 3rd Monday of February
        assert date(2026, 5, 25) in holidays  # Memorial Day, last Monday of May
        assert date(2026, 9, 7) in holidays  # Labor Day, 1st Monday of September
        assert date(2026, 11, 26) in holidays  # Thanksgiving, 4th Thursday of November

    def test_juneteenth_and_christmas_present(self):
        holidays = us_market_holidays(2026)
        assert date(2026, 6, 19) in holidays
        assert date(2026, 12, 25) in holidays


class TestIsTradingDay:
    def test_weekday_with_no_holiday_is_a_trading_day(self):
        assert is_trading_day(date(2026, 8, 24)) is True  # real Monday

    def test_saturday_is_not_a_trading_day(self):
        assert is_trading_day(date(2026, 8, 22)) is False  # real Saturday

    def test_sunday_is_not_a_trading_day(self):
        assert is_trading_day(date(2026, 8, 23)) is False  # real Sunday

    def test_observed_holiday_is_not_a_trading_day(self):
        # Independence Day 2026 observed Friday 2026-07-03 (see above).
        assert is_trading_day(date(2026, 7, 3)) is False

    def test_weekday_holiday_is_not_a_trading_day(self):
        assert is_trading_day(date(2026, 9, 7)) is False  # Labor Day, a real Monday


class TestPreviousNextTradingDay:
    def test_previous_trading_day_always_moves_back_at_least_one_day(self):
        # 2026-08-25 is itself a real trading day (Tuesday) -- the function
        # must still return an earlier date, not itself.
        assert previous_trading_day(date(2026, 8, 25)) == date(2026, 8, 24)

    def test_previous_trading_day_skips_a_weekend(self):
        # 2026-08-24 is a Monday -- previous real trading day skips Sat/Sun.
        assert previous_trading_day(date(2026, 8, 24)) == date(2026, 8, 21)

    def test_previous_trading_day_skips_holiday_and_weekend_together(self):
        # 2026-09-08 is a real Tuesday. Labor Day 2026-09-07 (Monday) and
        # the 2026-09-05/06 weekend both fall strictly before it -- the
        # previous real trading day is Friday 2026-09-04.
        assert previous_trading_day(date(2026, 9, 8)) == date(2026, 9, 4)

    def test_next_trading_day_always_moves_forward_at_least_one_day(self):
        assert next_trading_day(date(2026, 8, 24)) == date(2026, 8, 25)

    def test_next_trading_day_skips_a_weekend(self):
        # 2026-08-21 is a real Friday -- next real trading day skips Sat/Sun.
        assert next_trading_day(date(2026, 8, 21)) == date(2026, 8, 24)


class TestComputeEntryExitScheduleAmc:
    def test_amc_on_a_plain_weekday_enters_same_day_exits_next_day(self):
        # 2026-08-24 is a real Monday.
        result = compute_entry_exit_schedule(date(2026, 8, 24), AnnouncementTime.AFTER_MARKET)

        assert result.decision_generation_date == date(2026, 8, 24)
        assert result.exit_date == date(2026, 8, 25)
        assert result.entry_timestamp == datetime.combine(
            date(2026, 8, 24), ENTRY_EXIT_TIME, tzinfo=EASTERN
        )
        assert result.entry_timestamp.time() == time(15, 55)
        assert result.entry_timestamp.tzinfo == EASTERN
        assert "after-market-close" in result.reasoning.lower()

    def test_amc_on_a_friday_exits_the_following_monday(self):
        # 2026-08-21 is a real Friday -- exit must skip the weekend.
        result = compute_entry_exit_schedule(date(2026, 8, 21), AnnouncementTime.AFTER_MARKET)

        assert result.decision_generation_date == date(2026, 8, 21)
        assert result.exit_date == date(2026, 8, 24)

    def test_amc_reported_on_a_weekend_data_anomaly_falls_back_to_nearest_prior_trading_day(self):
        # 2026-08-22 is a real Saturday -- should never happen for a real
        # calendar entry, but must never be treated as if the market was
        # open that day.
        result = compute_entry_exit_schedule(date(2026, 8, 22), AnnouncementTime.AFTER_MARKET)

        assert result.decision_generation_date == date(2026, 8, 21)  # nearest trading day on/before
        assert result.exit_date == date(2026, 8, 24)


class TestComputeEntryExitScheduleBmo:
    def test_bmo_on_a_plain_weekday_enters_previous_day_exits_same_day(self):
        # 2026-08-25 is a real Tuesday, 2026-08-24 the real trading day before it.
        result = compute_entry_exit_schedule(date(2026, 8, 25), AnnouncementTime.BEFORE_MARKET)

        assert result.decision_generation_date == date(2026, 8, 24)
        assert result.exit_date == date(2026, 8, 25)
        assert result.entry_timestamp.time() == time(15, 55)
        assert result.entry_timestamp.tzinfo == EASTERN
        assert "look-ahead bias" in result.reasoning.lower()

    def test_bmo_the_day_after_a_holiday_weekend_enters_the_friday_before(self):
        # 2026-09-08 (Tuesday) BMO -- entry must be 2026-09-04 (Friday),
        # skipping Labor Day (2026-09-07) and the weekend.
        result = compute_entry_exit_schedule(date(2026, 9, 8), AnnouncementTime.BEFORE_MARKET)

        assert result.decision_generation_date == date(2026, 9, 4)
        assert result.exit_date == date(2026, 9, 8)

    def test_bmo_reported_on_a_weekend_data_anomaly_exits_nearest_following_trading_day(self):
        # 2026-08-22 is a real Saturday.
        result = compute_entry_exit_schedule(date(2026, 8, 22), AnnouncementTime.BEFORE_MARKET)

        assert result.decision_generation_date == date(2026, 8, 21)  # previous real trading day
        assert result.exit_date == date(2026, 8, 24)  # nearest trading day on/after


class TestComputeEntryExitScheduleUnknown:
    def test_unknown_session_matches_the_conservative_bmo_shaped_rule(self):
        amc_style_date = date(2026, 9, 8)
        unknown = compute_entry_exit_schedule(amc_style_date, AnnouncementTime.UNKNOWN)
        bmo = compute_entry_exit_schedule(amc_style_date, AnnouncementTime.BEFORE_MARKET)

        assert unknown.decision_generation_date == bmo.decision_generation_date
        assert unknown.exit_date == bmo.exit_date
        assert "never assume amc" in unknown.reasoning.lower()

    def test_unknown_never_takes_the_amc_shaped_entry_day(self):
        # If UNKNOWN were mistakenly treated as AMC, entry would be on the
        # earnings date itself instead of the day before -- assert it isn't.
        d = date(2026, 8, 25)
        unknown = compute_entry_exit_schedule(d, AnnouncementTime.UNKNOWN)

        assert unknown.decision_generation_date != d
        assert unknown.decision_generation_date == date(2026, 8, 24)
