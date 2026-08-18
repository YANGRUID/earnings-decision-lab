from datetime import UTC, datetime

from analytics.data_state import compute_options_data_state, compute_snapshot_age
from analytics.market_session import EASTERN
from models.enums import DataState


def _eastern(year, month, day, hour, minute=0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=EASTERN).astimezone(UTC)


# Wednesday 2026-03-18, 11:00 ET -- real regular market hours, used as
# "now" for every same-session-day scenario below.
_REGULAR_SESSION_NOW = _eastern(2026, 3, 18, 11, 0)


class TestComputeOptionsDataState:
    def test_no_snapshot_is_not_collected(self):
        assert (
            compute_options_data_state(None, None, _REGULAR_SESSION_NOW) == DataState.NOT_COLLECTED
        )

    def test_snapshot_from_a_prior_calendar_day_is_previous_session(self):
        snapshot = _eastern(2026, 3, 17, 15, 0)
        assert (
            compute_options_data_state(snapshot, "live", _REGULAR_SESSION_NOW)
            == DataState.PREVIOUS_SESSION
        )

    def test_previous_session_wins_even_with_a_live_quality_flag(self):
        # A same-provider quality flag from yesterday's fetch is meaningless
        # once the calendar day has rolled over -- the age, not the flag,
        # decides this case.
        snapshot = _eastern(2026, 3, 17, 9, 31)
        assert (
            compute_options_data_state(snapshot, "live", _REGULAR_SESSION_NOW)
            == DataState.PREVIOUS_SESSION
        )

    def test_same_day_snapshot_while_market_closed_is_market_closed(self):
        # Same real calendar day, but "now" is late at night -- market closed.
        now_closed = _eastern(2026, 3, 18, 22, 0)
        snapshot = _eastern(2026, 3, 18, 15, 0)
        assert (
            compute_options_data_state(snapshot, "live", now_closed) == DataState.MARKET_CLOSED
        )

    def test_same_day_market_open_live_quality(self):
        snapshot = _eastern(2026, 3, 18, 10, 55)
        assert (
            compute_options_data_state(snapshot, "live", _REGULAR_SESSION_NOW) == DataState.LIVE
        )

    def test_same_day_market_open_delayed_quality(self):
        snapshot = _eastern(2026, 3, 18, 10, 55)
        assert (
            compute_options_data_state(snapshot, "delayed", _REGULAR_SESSION_NOW)
            == DataState.DELAYED
        )

    def test_same_day_market_open_frozen_quality(self):
        snapshot = _eastern(2026, 3, 18, 10, 55)
        assert (
            compute_options_data_state(snapshot, "frozen", _REGULAR_SESSION_NOW)
            == DataState.FROZEN
        )

    def test_same_day_market_open_unrecognized_quality_is_unknown(self):
        snapshot = _eastern(2026, 3, 18, 10, 55)
        assert (
            compute_options_data_state(snapshot, "unavailable", _REGULAR_SESSION_NOW)
            == DataState.UNKNOWN
        )

    def test_same_day_market_open_missing_quality_is_unknown(self):
        snapshot = _eastern(2026, 3, 18, 10, 55)
        assert (
            compute_options_data_state(snapshot, None, _REGULAR_SESSION_NOW) == DataState.UNKNOWN
        )


class TestComputeSnapshotAge:
    def test_minutes_only_label_under_an_hour(self):
        snapshot = _REGULAR_SESSION_NOW.replace(minute=0)
        as_of = _REGULAR_SESSION_NOW.replace(minute=17)
        age = compute_snapshot_age(snapshot, as_of)
        assert age.minutes == 17
        assert age.label == "17m"

    def test_hours_and_minutes_label_over_an_hour(self):
        snapshot = _eastern(2026, 3, 18, 8, 6)
        as_of = _eastern(2026, 3, 18, 10, 30)
        age = compute_snapshot_age(snapshot, as_of)
        assert age.minutes == 144
        assert age.label == "2h 24m"

    def test_never_negative_when_snapshot_is_after_as_of(self):
        snapshot = _eastern(2026, 3, 18, 12, 0)
        as_of = _eastern(2026, 3, 18, 11, 0)
        age = compute_snapshot_age(snapshot, as_of)
        assert age.minutes == 0
        assert age.label == "0m"
