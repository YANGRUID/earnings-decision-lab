"""V4.2 Phase 2 in the forward window -- the flag, the boundary, the overlap.

Phase 2 is the most expensive thing in the 15:30 window: it opens real
market-data subscriptions across several expiries. These tests are about
whether it can be trusted to sit in that window at all -- whether it stays off
when it is off, whether it can reach backwards, and whether it can suppress
the two records that already exist.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from test_v4_2_multi_expiry_construction import FakeChainProvider
from test_v4_2_parallel_isolation import _control

from analytics.decision.v4_2_phase2_methodology import (
    PHASE_1_METHODOLOGY_V2,
    PHASE_2_METHODOLOGY,
)
from core.config import Settings, get_settings
from models.v4_2_challenger import V42ChallengerDecision
from services.v4_2_parallel import run_challenger_phase

D = Decimal
WINDOW = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)
LADDER = [date(2026, 9, 11), date(2026, 9, 18), date(2026, 9, 25), date(2026, 10, 16)]


def _settings(**overrides):
    return get_settings().model_copy(update=overrides)


def _phase2_on(**overrides):
    return _settings(
        v4_2_parallel_enabled=False,
        v4_2_independent_search_enabled=True,
        v4_2_independent_search_activation_at=WINDOW,
        **overrides,
    )


class TestTheFlag:
    def test_it_defaults_to_false(self):
        assert Settings(_env_file=None).v4_2_independent_search_enabled is False

    def test_it_is_a_separate_flag_from_phase_1(self):
        """Overloading Phase 1's flag would make 'which methodology produced
        this row?' unanswerable from configuration alone."""
        fields = Settings.model_fields
        assert "v4_2_parallel_enabled" in fields
        assert "v4_2_independent_search_enabled" in fields

    def test_with_the_flag_off_nothing_is_searched_and_nothing_is_written(self, db_session):
        _control(db_session, "P2OFF")
        provider = FakeChainProvider(LADDER)
        before = db_session.query(V42ChallengerDecision).count()

        summary = run_challenger_phase(
            db_session,
            _settings(v4_2_parallel_enabled=False, v4_2_independent_search_enabled=False),
            provider=provider,
            now=WINDOW,
        )
        db_session.flush()

        assert summary.phase2_enabled is False
        assert summary.phase2_evaluated == 0
        assert provider.underlying_calls == 0, "a disabled phase issued a market-data request"
        assert provider.metadata_calls == 0
        assert db_session.query(V42ChallengerDecision).count() == before


class TestTheActivationBoundary:
    def test_the_flag_alone_activates_nothing(self, db_session):
        """Turning the flag on must not retroactively produce rows for events
        already past. Without an activation instant, no event qualifies."""
        _control(db_session, "P2NOAC")
        provider = FakeChainProvider(LADDER)

        summary = run_challenger_phase(
            db_session,
            _settings(
                v4_2_parallel_enabled=False,
                v4_2_independent_search_enabled=True,
                v4_2_independent_search_activation_at=None,
            ),
            provider=provider,
            now=WINDOW,
        )
        db_session.flush()

        assert summary.phase2_evaluated == 0
        assert summary.phase2_skipped_not_activated == 1
        assert summary.phase2_by_event["P2NOAC"] == "NOT_ACTIVATED"
        assert provider.underlying_calls == 0

    def test_an_event_whose_window_precedes_activation_is_skipped(self, db_session):
        _control(db_session, "P2EARLY")
        provider = FakeChainProvider(LADDER)

        summary = run_challenger_phase(
            db_session,
            _settings(
                v4_2_parallel_enabled=False,
                v4_2_independent_search_enabled=True,
                v4_2_independent_search_activation_at=WINDOW.replace(hour=20),
            ),
            provider=provider,
            now=WINDOW,
        )
        db_session.flush()

        assert summary.phase2_evaluated == 0
        assert summary.phase2_skipped_not_activated == 1
        assert db_session.query(V42ChallengerDecision).count() == 0

    def test_the_boundary_is_checked_against_the_legal_window_not_the_clock(self, db_session):
        """A retried or late run must not admit an event whose legal decision
        window opened before activation, however late 'now' happens to be."""
        _control(db_session, "P2LATE", generated_at=WINDOW)
        provider = FakeChainProvider(LADDER)

        summary = run_challenger_phase(
            db_session,
            _settings(
                v4_2_parallel_enabled=False,
                v4_2_independent_search_enabled=True,
                v4_2_independent_search_activation_at=WINDOW.replace(hour=19, minute=45),
            ),
            provider=provider,
            now=WINDOW.replace(hour=23),
        )
        db_session.flush()

        assert summary.phase2_skipped_not_activated == 1
        assert summary.phase2_evaluated == 0


class TestTheOverlapPeriod:
    def test_phase_2_searches_and_freezes_an_activated_event(self, db_session):
        _control(db_session, "P2GO")
        provider = FakeChainProvider(LADDER)

        summary = run_challenger_phase(
            db_session, _phase2_on(), provider=provider, now=WINDOW,
            settlement_date=date(2026, 9, 11),
        )
        db_session.flush()

        assert summary.phase2_evaluated == 1
        assert summary.phase2_expiries_searched > 1, "Phase 2 searched a single expiry"
        assert provider.underlying_calls == 1
        rows = db_session.query(V42ChallengerDecision).all()
        assert len(rows) == 1
        assert rows[0].methodology_version == PHASE_2_METHODOLOGY
        assert rows[0].expiries_considered > 1

    def test_both_phases_record_against_the_same_control(self, db_session):
        """Section 97's overlap: Phase 1's restraint baseline and Phase 2's
        independent search, on the same natural event."""
        _control(db_session, "P2BOTH")
        provider = FakeChainProvider(LADDER)

        summary = run_challenger_phase(
            db_session,
            _settings(
                v4_2_parallel_enabled=True,
                v4_2_parallel_activation_at=WINDOW,
                v4_2_independent_search_enabled=True,
                v4_2_independent_search_activation_at=WINDOW,
            ),
            provider=provider,
            now=WINDOW,
            settlement_date=date(2026, 9, 11),
        )
        db_session.flush()

        assert summary.evaluated == 1
        assert summary.phase2_evaluated == 1
        rows = db_session.query(V42ChallengerDecision).all()
        assert len(rows) == 2
        assert {r.methodology_version for r in rows} == {
            PHASE_1_METHODOLOGY_V2,
            PHASE_2_METHODOLOGY,
        }

    def test_phase_1_still_runs_when_phase_2_is_off(self, db_session):
        _control(db_session, "P1ONLY")

        summary = run_challenger_phase(
            db_session,
            _settings(
                v4_2_parallel_enabled=True,
                v4_2_parallel_activation_at=WINDOW,
                v4_2_independent_search_enabled=False,
            ),
            provider=None,
            now=WINDOW,
        )
        db_session.flush()

        assert summary.evaluated == 1
        assert summary.phase2_evaluated == 0
        rows = db_session.query(V42ChallengerDecision).all()
        assert len(rows) == 1
        assert rows[0].methodology_version == PHASE_1_METHODOLOGY_V2

    def test_a_phase_2_fault_is_recorded_and_never_stops_phase_1(self, db_session):
        class Broken:
            def get_underlying_quote(self, ticker):
                raise RuntimeError("TWS dropped")

        _control(db_session, "P2BREAK")

        summary = run_challenger_phase(
            db_session,
            _settings(
                v4_2_parallel_enabled=True,
                v4_2_parallel_activation_at=WINDOW,
                v4_2_independent_search_enabled=True,
                v4_2_independent_search_activation_at=WINDOW,
            ),
            provider=Broken(),
            now=WINDOW,
        )
        db_session.flush()

        assert summary.evaluated == 1, "Phase 1 was stopped by a Phase-2 fault"
        assert summary.phase2_evaluated == 1
        assert summary.phase2_failed == 1
        # Phase 1's own row exists and is untouched.
        rows = db_session.query(V42ChallengerDecision).all()
        assert any(r.methodology_version == PHASE_1_METHODOLOGY_V2 for r in rows)


class TestRerunsAreIdempotent:
    def test_a_second_run_in_the_same_window_adds_nothing(self, db_session):
        _control(db_session, "P2TWICE")
        provider = FakeChainProvider(LADDER)
        settings = _phase2_on()

        run_challenger_phase(db_session, settings, provider=provider, now=WINDOW,
                             settlement_date=date(2026, 9, 11))
        db_session.flush()
        first = db_session.query(V42ChallengerDecision).count()
        calls = provider.underlying_calls

        run_challenger_phase(db_session, settings, provider=provider, now=WINDOW,
                             settlement_date=date(2026, 9, 11))
        db_session.flush()

        assert db_session.query(V42ChallengerDecision).count() == first
        assert provider.underlying_calls == calls, (
            "a rerun re-acquired market data for an event already frozen"
        )
