"""V4.2 Phase 2 -- can it fit inside the window it has to run in?

Phase 1 cost nothing: it re-read the control's frozen rows and issued zero
market-data requests, so no amount of events could overrun the window. Phase 2
opens real subscriptions across a bounded ladder for every event, so "does it
fit?" is now a question with a wrong answer.

These measure the shape of the growth on deterministic fixtures -- requests
per event, contracts per event -- and prove the two bounds that keep it
finite: the ladder cap, and the window deadline.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from test_v4_2_multi_expiry_construction import FakeChainProvider
from test_v4_2_parallel_isolation import _control

from core.config import get_settings
from models.v4_2_challenger import V42ChallengerDecision
from services.v4_2_parallel import run_challenger_phase

D = Decimal
WINDOW = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)
LADDER = [date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25), date(2026, 10, 16)]


class CountingProvider(FakeChainProvider):
    """Every provider call is a request. Counted, never estimated."""

    @property
    def total_requests(self) -> int:
        return (
            self.underlying_calls
            + self.metadata_calls
            + len(self.chain_calls)
            + len(self.quote_calls)
        )


def _phase2_settings(**overrides):
    return get_settings().model_copy(
        update={
            "v4_2_parallel_enabled": False,
            "v4_2_independent_search_enabled": True,
            "v4_2_independent_search_activation_at": WINDOW,
            **overrides,
        }
    )


def _run(
    db_session,
    events: int,
    *,
    prefix: str = "CAP",
    max_expiries: int = 3,
    deadline=None,
    clock=None,
):
    for index in range(events):
        _control(db_session, f"{prefix}{index:02d}")
    provider = CountingProvider(LADDER)
    summary = run_challenger_phase(
        db_session,
        _phase2_settings(v4_2_independent_search_max_expiries=max_expiries),
        provider=provider,
        now=WINDOW,
        settlement_date=date(2026, 9, 11),
        deadline=deadline,
        clock=clock,
    )
    db_session.flush()
    return summary, provider


class TestTheWorkloadGrowsLinearlyAndStaysBounded:
    @pytest.mark.parametrize("events", [1, 4, 8])
    def test_every_event_costs_the_same_bounded_budget(self, db_session, events):
        summary, provider = _run(db_session, events)

        assert summary.phase2_evaluated == events
        assert summary.phase2_failed == 0
        # One underlying and one metadata call per event; one chain discovery
        # and one selected-leg quote call per LADDER RUNG per event. Never per
        # configuration, and never per candidate.
        assert provider.underlying_calls == events
        assert provider.metadata_calls == events
        assert len(provider.chain_calls) <= 3 * events
        assert len(provider.quote_calls) <= 3 * events
        assert provider.total_requests <= 8 * events

    def test_eight_events_cost_eight_times_one_event(self, db_session):
        """Linear, not quadratic: nothing in the search is shared ACROSS
        events, and nothing about one event's universe grows with how many
        other events are in the window."""
        one, provider_one = _run(db_session, 1, prefix="LIN1")
        per_event = provider_one.total_requests

        # A fresh set of controls in the same session.
        eight, provider_eight = _run(db_session, 8, prefix="LIN8")

        assert one.phase2_evaluated == 1
        assert eight.phase2_evaluated == 8
        assert provider_eight.total_requests == per_event * 8

    def test_a_stress_workload_still_terminates_within_the_same_bound(self, db_session):
        """Twenty events is well past any real earnings day this project has
        seen -- the busiest so far put eight through one window."""
        summary, provider = _run(db_session, 20)

        assert summary.phase2_evaluated == 20
        assert summary.phase2_failed == 0
        assert provider.total_requests <= 8 * 20
        assert db_session.query(V42ChallengerDecision).count() == 20

    def test_narrowing_the_ladder_narrows_the_budget(self, db_session):
        """The operator-facing lever, so capacity can be proven before it is
        needed rather than discovered in a window."""
        three, provider_three = _run(db_session, 4, prefix="LAD3", max_expiries=3)
        one, provider_one = _run(db_session, 4, prefix="LAD1", max_expiries=1)

        assert three.phase2_evaluated == one.phase2_evaluated == 4
        assert provider_one.total_requests < provider_three.total_requests


class TestTheDeadlineIsBinding:
    def test_no_new_event_starts_after_the_deadline(self, db_session):
        summary, provider = _run(
            db_session, 4, deadline=WINDOW, clock=lambda: WINDOW + timedelta(minutes=1)
        )

        assert summary.phase2_evaluated == 0
        assert summary.phase2_deadline_skipped == 4
        assert provider.total_requests == 0, (
            "a market-data request was issued after the window deadline"
        )
        assert set(summary.phase2_by_event.values()) == {"DEADLINE_SKIPPED"}

    def test_a_deadline_still_ahead_does_not_skip_anything(self, db_session):
        summary, _ = _run(
            db_session, 3, deadline=WINDOW + timedelta(minutes=20), clock=lambda: WINDOW
        )

        assert summary.phase2_evaluated == 3
        assert summary.phase2_deadline_skipped == 0

    def test_an_event_already_started_is_always_finished(self, db_session):
        """The deadline stops STARTS, never abandons work in flight: a
        half-evaluated event would leave the most expensive thing in the
        window paid for and unrecorded."""
        for index in range(3):
            _control(db_session, f"DL{index:02d}")
        provider = CountingProvider(LADDER)
        ticks = iter(
            [WINDOW, WINDOW, WINDOW + timedelta(minutes=30), WINDOW + timedelta(minutes=30)]
        )

        summary = run_challenger_phase(
            db_session,
            _phase2_settings(),
            provider=provider,
            now=WINDOW,
            settlement_date=date(2026, 9, 11),
            deadline=WINDOW + timedelta(minutes=10),
            clock=lambda: next(ticks, WINDOW + timedelta(minutes=30)),
        )
        db_session.flush()

        assert summary.phase2_evaluated == 2
        assert summary.phase2_deadline_skipped == 1
        # Both started events produced a complete record, not a partial one.
        rows = db_session.query(V42ChallengerDecision).all()
        assert len(rows) == 2
        assert all(r.expiries_considered and r.candidates_evaluated for r in rows)
