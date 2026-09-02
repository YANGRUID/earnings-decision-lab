"""V4 15:30 ET decision timing policy (Sections 21-26, 54-56, 61).

Proves the new V4 entry clock is real, that V3's clock did not move with
it, and that settlement timing did not move with entry timing.
"""

from datetime import date, time

import pytest

from analytics.decision_timing_policy import (
    V3_TIMING_POLICY,
    V4_TIMING_POLICY,
    get_timing_policy,
)
from analytics.earnings_timing import compute_entry_exit_schedule, is_trading_day
from models.enums import AnnouncementTime


class TestPolicyIdentities:
    def test_v3_entry_and_exit_are_both_1555(self):
        assert V3_TIMING_POLICY.entry_time == time(15, 55)
        assert V3_TIMING_POLICY.exit_time == time(15, 55)

    def test_v4_moves_entry_to_1530_but_leaves_exit_at_1555(self):
        """Section 56 -- entry timing and settlement timing are separate
        policies. The whole point of the V4 policy is that only one of
        them moved."""
        assert V4_TIMING_POLICY.entry_time == time(15, 30)
        assert V4_TIMING_POLICY.exit_time == time(15, 55)

    def test_v4_observes_earlier_than_v3_never_later(self):
        """No post-event advantage: V4 sees strictly less of the session."""
        assert V4_TIMING_POLICY.entry_time < V3_TIMING_POLICY.entry_time

    def test_versions_are_distinct_and_resolvable(self):
        assert V3_TIMING_POLICY.version != V4_TIMING_POLICY.version
        assert get_timing_policy(V3_TIMING_POLICY.version) is V3_TIMING_POLICY
        assert get_timing_policy(V4_TIMING_POLICY.version) is V4_TIMING_POLICY

    def test_unknown_version_raises_rather_than_defaulting(self):
        """A record whose policy is unknown must never be silently
        reinterpreted under some other cohort's clock."""
        with pytest.raises(ValueError, match="Unknown decision timing policy"):
            get_timing_policy("v9-does-not-exist")


class TestScheduleUnderEachPolicy:
    def test_default_is_still_v3_so_existing_callers_are_unchanged(self):
        """Section 22 -- the policy parameter is additive. Every caller
        that does not pass one must get exactly the previous behaviour."""
        explicit = compute_entry_exit_schedule(
            date(2026, 9, 10), AnnouncementTime.AFTER_MARKET, policy=V3_TIMING_POLICY
        )
        implicit = compute_entry_exit_schedule(date(2026, 9, 10), AnnouncementTime.AFTER_MARKET)
        assert implicit.entry_timestamp == explicit.entry_timestamp
        assert implicit.exit_timestamp == explicit.exit_timestamp
        assert implicit.entry_timestamp.hour == 15
        assert implicit.entry_timestamp.minute == 55

    def test_amc_reports_same_day_at_1530_under_v4(self):
        """Section 24 -- AMC: same reporting day, ~15:30 ET."""
        s = compute_entry_exit_schedule(
            date(2026, 9, 10), AnnouncementTime.AFTER_MARKET, policy=V4_TIMING_POLICY
        )
        assert s.decision_generation_date == date(2026, 9, 10)  # Thursday, same day
        assert (s.entry_timestamp.hour, s.entry_timestamp.minute) == (15, 30)

    def test_bmo_reports_previous_trading_day_at_1530_under_v4(self):
        """Section 24 -- BMO: previous trading day, ~15:30 ET. Entering on
        the earnings date itself would be look-ahead bias."""
        s = compute_entry_exit_schedule(
            date(2026, 9, 10), AnnouncementTime.BEFORE_MARKET, policy=V4_TIMING_POLICY
        )
        assert s.decision_generation_date == date(2026, 9, 9)
        assert (s.entry_timestamp.hour, s.entry_timestamp.minute) == (15, 30)

    def test_settlement_stays_1555_under_v4(self):
        """Section 56 -- the exit observation must NOT have moved."""
        for session in (AnnouncementTime.AFTER_MARKET, AnnouncementTime.BEFORE_MARKET):
            s = compute_entry_exit_schedule(
                date(2026, 9, 10), session, policy=V4_TIMING_POLICY
            )
            assert (s.exit_timestamp.hour, s.exit_timestamp.minute) == (15, 55), session

    def test_unknown_session_takes_the_conservative_bmo_branch(self):
        """Never assume AMC -- an unknown announcement time must not buy
        an extra day of pre-release data."""
        s = compute_entry_exit_schedule(
            date(2026, 9, 10), AnnouncementTime.UNKNOWN, policy=V4_TIMING_POLICY
        )
        assert s.decision_generation_date == date(2026, 9, 9)
        assert (s.entry_timestamp.hour, s.entry_timestamp.minute) == (15, 30)


class TestTradingCalendarUnderV4:
    def test_weekend_amc_rolls_back_to_a_real_trading_day(self):
        """2026-09-12 is a Saturday. The decision date must be a real
        session, not the calendar date -- and still at 15:30."""
        assert not is_trading_day(date(2026, 9, 12))
        s = compute_entry_exit_schedule(
            date(2026, 9, 12), AnnouncementTime.AFTER_MARKET, policy=V4_TIMING_POLICY
        )
        assert is_trading_day(s.decision_generation_date)
        assert s.decision_generation_date == date(2026, 9, 11)  # Friday
        assert (s.entry_timestamp.hour, s.entry_timestamp.minute) == (15, 30)

    def test_holiday_bmo_skips_the_market_holiday(self):
        """2026-07-03 is the observed Independence Day holiday (July 4
        falls on a Saturday). A BMO report on the next session must look
        back past the holiday AND past the weekend."""
        assert not is_trading_day(date(2026, 7, 3))
        s = compute_entry_exit_schedule(
            date(2026, 7, 6), AnnouncementTime.BEFORE_MARKET, policy=V4_TIMING_POLICY
        )
        assert is_trading_day(s.decision_generation_date)
        assert s.decision_generation_date == date(2026, 7, 2)  # Thursday
        assert (s.entry_timestamp.hour, s.entry_timestamp.minute) == (15, 30)

    def test_exit_lands_on_a_real_trading_day(self):
        s = compute_entry_exit_schedule(
            date(2026, 9, 11), AnnouncementTime.AFTER_MARKET, policy=V4_TIMING_POLICY
        )
        assert is_trading_day(s.exit_date)
        assert s.exit_date == date(2026, 9, 14)  # Friday AMC -> Monday exit


class TestNo1555LeakageIntoV4:
    def test_v4_entry_timestamps_never_carry_1555(self):
        """Section 61 -- no 15:55 leakage into V4 timing policy."""
        for day in range(1, 29):
            for session in (
                AnnouncementTime.AFTER_MARKET,
                AnnouncementTime.BEFORE_MARKET,
                AnnouncementTime.UNKNOWN,
            ):
                s = compute_entry_exit_schedule(
                    date(2026, 9, day), session, policy=V4_TIMING_POLICY
                )
                assert (s.entry_timestamp.hour, s.entry_timestamp.minute) == (15, 30)

    def test_v4_reasoning_text_says_1530_not_1555(self):
        """The human-readable reasoning is persisted evidence. Under a V4
        policy, text claiming 15:55 would be a false record."""
        s = compute_entry_exit_schedule(
            date(2026, 9, 10), AnnouncementTime.AFTER_MARKET, policy=V4_TIMING_POLICY
        )
        assert "15:30 ET" in s.reasoning
        # 15:55 may still appear -- as the EXIT time, which is correct.
        assert "entered at 15:30 ET" in s.reasoning
