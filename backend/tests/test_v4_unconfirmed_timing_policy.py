"""An unconfirmed announcement session never produces a forward observation.

The defect (2026-09-17 hardening). v1/v2 scheduled an UNKNOWN session as if it
were BMO -- decide D-1 15:30, settle D 15:30 -- on the reasoning that entering a
day early is never look-ahead. True of the entry, and false of the settlement:
if the company actually reports after D's close, the release happens AFTER the
settlement observation, so the observation spans no earnings at all and is then
graded and published as an earnings result.

These prove the block itself, that it is governed by the versioned policy, that
a confirmed session is unaffected, and that a session confirmed too late leaves
a missed window rather than a late decision.
"""

from datetime import UTC, datetime, time

from analytics.decision_timing_policy import (
    V4_ACTIVE_TIMING_POLICY,
    V4_TIMING_POLICY_V2,
    V4_TIMING_POLICY_V3,
    get_timing_policy,
)
from analytics.forward_windows import timing_is_confirmed, unconfirmed_timing_reason
from models.enums import EarningsTiming
from services.v4_shadow_orchestration import TIMING_UNCONFIRMED, _timing_confirmation_block


class _Event:
    """The only attribute the block reads."""

    def __init__(self, timing: EarningsTiming) -> None:
        self.earnings_time = timing


class TestTheVersionedPolicy:
    def test_v3_is_active_and_requires_a_confirmed_session(self):
        assert V4_ACTIVE_TIMING_POLICY is V4_TIMING_POLICY_V3
        assert V4_TIMING_POLICY_V3.requires_confirmed_timing is True

    def test_v2_did_not_require_one_and_is_still_resolvable(self):
        """Rows frozen under v2 keep their own version and their own rule --
        the change is prospective, never applied backwards to evidence."""
        assert V4_TIMING_POLICY_V2.requires_confirmed_timing is False
        assert get_timing_policy(V4_TIMING_POLICY_V2.version) is V4_TIMING_POLICY_V2

    def test_v3_did_not_move_the_observation_clock(self):
        """Only the eligibility rule changed. If the clock had moved with it,
        v3 would be a second, silent change to what every observation means."""
        assert V4_TIMING_POLICY_V3.entry_time == V4_TIMING_POLICY_V2.entry_time == time(15, 30)
        assert V4_TIMING_POLICY_V3.exit_time == V4_TIMING_POLICY_V2.exit_time == time(15, 30)


class TestWhichSessionsAreConfirmed:
    def test_bmo_and_amc_are_confirmed(self):
        assert timing_is_confirmed(_Event(EarningsTiming.BMO))
        assert timing_is_confirmed(_Event(EarningsTiming.AMC))

    def test_unknown_and_during_market_hours_are_not(self):
        assert not timing_is_confirmed(_Event(EarningsTiming.UNKNOWN))
        assert not timing_is_confirmed(_Event(EarningsTiming.DMH))

    def test_a_raw_stored_value_is_read_the_same_way(self):
        """A freshly flushed ORM row carries the raw name, not the enum."""
        assert timing_is_confirmed(_Event("BMO"))
        assert not timing_is_confirmed(_Event("UNKNOWN"))

    def test_each_unconfirmed_session_explains_itself_in_its_own_terms(self):
        assert "before the open or after the close" in unconfirmed_timing_reason(
            _Event(EarningsTiming.UNKNOWN)
        )
        assert "during market hours" in unconfirmed_timing_reason(_Event(EarningsTiming.DMH))


class TestTheDecisionGate:
    def test_an_unknown_session_is_blocked(self):
        blocked = _timing_confirmation_block(_Event(EarningsTiming.UNKNOWN))
        assert blocked is not None
        assert blocked[0] == TIMING_UNCONFIRMED

    def test_the_block_is_never_research_not_ready_or_a_failure(self):
        """The whole point of a distinct category: research may be perfectly
        ready, and nothing here failed."""
        category, reason = _timing_confirmation_block(_Event(EarningsTiming.UNKNOWN))
        assert category not in {"RESEARCH_NOT_READY", "DECISION_FAILED", "NOT_ELIGIBLE"}
        assert "no decision is generated" in reason.lower()

    def test_a_confirmed_session_passes_the_gate_untouched(self):
        assert _timing_confirmation_block(_Event(EarningsTiming.BMO)) is None
        assert _timing_confirmation_block(_Event(EarningsTiming.AMC)) is None

    def test_a_session_that_becomes_confirmed_is_no_longer_blocked(self):
        """Resolution needs no intervention: the same event, once the calendar
        corroborates a session, simply stops being blocked."""
        event = _Event(EarningsTiming.UNKNOWN)
        assert _timing_confirmation_block(event) is not None
        event.earnings_time = EarningsTiming.AMC
        assert _timing_confirmation_block(event) is None

    def test_a_policy_without_the_rule_blocks_nothing(self, monkeypatch):
        """Governed by the active policy, not by a hidden constant -- so what a
        given run applied is recoverable from the version it froze."""
        monkeypatch.setattr(
            "services.v4_shadow_orchestration.V4_ACTIVE_TIMING_POLICY", V4_TIMING_POLICY_V2
        )
        assert _timing_confirmation_block(_Event(EarningsTiming.UNKNOWN)) is None


class TestTheReadModel:
    """A blocked event must read as blocked, and -- once its window has gone --
    as a missed window, never as a decision that could still be written."""

    def _event(self, db, *, symbol: str, timing: EarningsTiming, when):
        from decimal import Decimal

        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.enums import EarningsCalendarEventStatus, EarningsSource

        row = EarningsCalendarEvent(
            symbol=symbol,
            company_name=f"{symbol} Inc",
            earnings_date=when,
            earnings_time=timing,
            status=EarningsCalendarEventStatus.UPCOMING,
            source=EarningsSource.FINNHUB,
            market_cap=Decimal("50000000000"),
            country="US",
            created_at=datetime(2031, 5, 1, tzinfo=UTC),
        )
        db.add(row)
        db.flush()
        return row

    def test_a_future_unconfirmed_event_reads_as_timing_unconfirmed(self, db_session):
        from datetime import date

        from services.operations import STATE_TIMING_UNCONFIRMED, classify_event

        event = self._event(
            db_session, symbol="TESTUNK", timing=EarningsTiming.UNKNOWN, when=date(2031, 5, 20)
        )
        row = classify_event(db_session, event, datetime(2031, 5, 15, 12, 0, tzinfo=UTC))
        assert row.lifecycle_state == STATE_TIMING_UNCONFIRMED
        assert row.next_action == "Corroborate the earnings session (BMO or AMC)"

    def test_a_session_confirmed_only_after_the_window_leaves_it_missed(self, db_session):
        """Requirement: never a decision written after its legal window. The
        state says the window was missed AND why, so it is not confused with a
        pipeline that simply never ran."""
        from datetime import date

        from services.operations import STATE_WINDOW_MISSED_TIMING_UNCONFIRMED, classify_event

        event = self._event(
            db_session, symbol="TESTLATE", timing=EarningsTiming.UNKNOWN, when=date(2031, 5, 20)
        )
        row = classify_event(db_session, event, datetime(2031, 5, 30, 12, 0, tzinfo=UTC))
        assert row.lifecycle_state == STATE_WINDOW_MISSED_TIMING_UNCONFIRMED
        assert row.shadow_decision_id is None

    def test_a_confirmed_future_event_is_not_blocked(self, db_session):
        from datetime import date

        from services.operations import (
            STATE_TIMING_UNCONFIRMED,
            STATE_WINDOW_MISSED_TIMING_UNCONFIRMED,
            classify_event,
        )

        event = self._event(
            db_session, symbol="TESTBMO", timing=EarningsTiming.BMO, when=date(2031, 5, 20)
        )
        row = classify_event(db_session, event, datetime(2031, 5, 15, 12, 0, tzinfo=UTC))
        assert row.lifecycle_state not in {
            STATE_TIMING_UNCONFIRMED,
            STATE_WINDOW_MISSED_TIMING_UNCONFIRMED,
        }
