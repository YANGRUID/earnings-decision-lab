"""Activation phase (Sections 51-53): the V4 shadow jobs observe under the
V4 timing policy's OWN windows.

Found live on 2026-09-02 at 12:35 ET, before activation: the shadow
decision job reused V3's due predicate (keyed to V3's 15:55 entry), so at
the 15:30 cron it selected 0 of the 34 events V3 would see at 15:55; and
the settlement job had no exit-window guard, so it would have settled a
15:30 entry at 15:55 the same afternoon, before the announcement.

Non-vacuity: every window test asserts both the positive and the negative
side of each boundary, and the DB-backed tests assert real settlement rows
were (or were not) written and that the provider was (or was not) called.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from models.enums import EarningsTiming
from models.v4_shadow import V4ShadowConfigEntry, V4ShadowConfigSettlement, V4ShadowRunEvent
from services.v4_shadow_scheduler import (
    SETTLEMENT_DUE,
    SETTLEMENT_NOT_DUE,
    SETTLEMENT_WINDOW_MISSED,
    due_for_v4_decision_now,
    settle_due_cohorts,
    v4_schedule_for_event,
    v4_settlement_window_state,
)

ET = ZoneInfo("America/New_York")


def _et(y, m, d, hh, mm):
    return datetime(y, m, d, hh, mm, tzinfo=ET)


def _event(earnings_date, timing):
    return SimpleNamespace(earnings_date=earnings_date, earnings_time=timing)


# 2026-09-10 is a Thursday; 09-11 a Friday.
AMC_THU = _event(date(2026, 9, 10), EarningsTiming.AMC)


class TestDecisionWindow:
    def test_amc_event_is_due_at_1530_under_v4_and_not_at_v3s_1555(self):

        assert due_for_v4_decision_now(AMC_THU, _et(2026, 9, 10, 15, 30))
        assert due_for_v4_decision_now(AMC_THU, _et(2026, 9, 10, 15, 35))  # + LATE_CUTOFF_GRACE
        assert not due_for_v4_decision_now(AMC_THU, _et(2026, 9, 10, 15, 29))
        assert not due_for_v4_decision_now(AMC_THU, _et(2026, 9, 10, 15, 36))
        assert not due_for_v4_decision_now(AMC_THU, _et(2026, 9, 10, 15, 55))

    @pytest.mark.parametrize("timing", [EarningsTiming.BMO, EarningsTiming.UNKNOWN])
    def test_bmo_and_unknown_use_previous_trading_day(self, timing):
        ev = _event(date(2026, 9, 11), timing)
        assert due_for_v4_decision_now(ev, _et(2026, 9, 10, 15, 30))
        assert not due_for_v4_decision_now(ev, _et(2026, 9, 11, 15, 30))

    def test_raw_enum_name_string_is_tolerated(self):
        """A freshly flushed ORM row can still carry the raw name string."""
        raw = _event(date(2026, 9, 10), "AMC")
        assert due_for_v4_decision_now(raw, _et(2026, 9, 10, 15, 30))

    def test_schedule_is_v3s_day_with_v4s_clock(self):

        s = v4_schedule_for_event(AMC_THU)
        assert s.decision_generation_date == date(2026, 9, 10)
        assert s.entry_timestamp == _et(2026, 9, 10, 15, 30)
        assert s.exit_timestamp == _et(2026, 9, 11, 15, 30)  # v2: T+1 15:30 ET


class TestSettlementWindow:
    def test_same_afternoon_is_not_due(self):
        assert v4_settlement_window_state(AMC_THU, _et(2026, 9, 10, 15, 55)) == SETTLEMENT_NOT_DUE
        assert v4_settlement_window_state(AMC_THU, _et(2026, 9, 11, 15, 24)) == SETTLEMENT_NOT_DUE

    def test_exit_window_bounds(self):
        # EARLY_CAPTURE_TOLERANCE (5 min) before, LATE_CUTOFF_GRACE (5 min) after 15:30 ET T+1.
        assert v4_settlement_window_state(AMC_THU, _et(2026, 9, 11, 15, 25)) == SETTLEMENT_DUE
        assert v4_settlement_window_state(AMC_THU, _et(2026, 9, 11, 15, 30)) == SETTLEMENT_DUE
        assert v4_settlement_window_state(AMC_THU, _et(2026, 9, 11, 15, 35)) == SETTLEMENT_DUE
        assert (
            v4_settlement_window_state(AMC_THU, _et(2026, 9, 11, 15, 36))
            == SETTLEMENT_WINDOW_MISSED
        )

    def test_bmo_event_settles_on_its_own_day(self):
        ev = _event(date(2026, 9, 11), EarningsTiming.BMO)
        assert v4_settlement_window_state(ev, _et(2026, 9, 10, 15, 55)) == SETTLEMENT_NOT_DUE
        assert v4_settlement_window_state(ev, _et(2026, 9, 11, 15, 30)) == SETTLEMENT_DUE


# --------------------------------------------------------------------------
# DB-backed: the settlement orchestration over real frozen cohorts.
# --------------------------------------------------------------------------


class _RaisingProvider:
    """Any quote request is a test failure: NOT_DUE and MISSED must never quote."""

    def get_quotes_for_known_contracts(self, *a, **k):
        raise AssertionError("provider must not be called")


class _QuotingProvider:
    def __init__(self):
        self.calls = []

    def get_quotes_for_known_contracts(self, ticker, contracts, expiration, observed_at):
        self.calls.append([c.external_contract_id for c in contracts])
        price = {
            "c100": ("6.00", "6.20"),
            "c105": ("1.50", "1.60"),
            "p95": ("9.00", "9.20"),
            "c110w": ("41.00", "41.20"),
            "c160": ("5.50", "5.60"),
        }
        return [
            SimpleNamespace(
                strike=c.strike,
                option_type=c.option_type,
                bid=Decimal(price[c.external_contract_id][0]),
                ask=Decimal(price[c.external_contract_id][1]),
                market_data_quality="delayed",
                retrieved_at=observed_at,
            )
            for c in contracts
        ]


@pytest.fixture
def frozen(db_session, monkeypatch):
    """One real six-cohort decision for an AMC event on Thu 2026-09-10,
    frozen at 15:30 ET, via the cohort test module's own freeze helper."""
    import test_v4_six_cohort_evidence as cohort

    from models.earnings_calendar_event import EarningsCalendarEvent

    event = EarningsCalendarEvent(
        symbol="SIXC",
        company_name="Six Cohort Co",
        earnings_date=date(2026, 9, 10),
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    result = cohort._freeze(db_session, event, monkeypatch=monkeypatch)
    assert result.status == "RANKED", result
    db_session.flush()
    observed = (
        db_session.query(V4ShadowConfigEntry)
        .filter_by(shadow_decision_id=result.decision_id, status="OBSERVED")
        .count()
    )
    assert observed >= 3, "non-vacuity: the fixture must hold real observed positions"
    return SimpleNamespace(decision_id=result.decision_id, observed=observed)


def _settlements(db, decision_id):
    return db.query(V4ShadowConfigSettlement).filter_by(shadow_decision_id=decision_id).all()


class TestSettleDueCohorts:
    def test_same_afternoon_run_leaves_positions_pending_and_never_quotes(self, db_session, frozen):
        # The 15:55 ET settlement cron on the entry day itself.
        s = settle_due_cohorts(
            db_session, provider=_RaisingProvider(), now=_et(2026, 9, 10, 15, 55)
        )
        assert (s.not_due, s.evaluated, s.settled, s.failed) == (1, 0, 0, 0)
        assert _settlements(db_session, frozen.decision_id) == []

    def test_t_plus_one_window_settles_every_observed_position_with_one_quote_call(
        self, db_session, frozen
    ):
        provider = _QuotingProvider()
        s = settle_due_cohorts(db_session, provider=provider, now=_et(2026, 9, 11, 15, 30))
        assert (s.not_due, s.evaluated, s.failed) == (0, 1, 0)
        assert s.settled == frozen.observed
        rows = _settlements(db_session, frozen.decision_id)
        assert {r.status for r in rows} == {"SETTLED"}
        assert len(provider.calls) == 1  # one expiration group, one call, six configs

    def test_missed_window_closes_positions_as_terminal_failures_without_quoting(
        self, db_session, frozen
    ):
        s = settle_due_cohorts(db_session, provider=_RaisingProvider(), now=_et(2026, 9, 11, 16, 1))
        assert (s.not_due, s.evaluated, s.settled) == (0, 1, 0)
        assert s.failed == frozen.observed
        rows = _settlements(db_session, frozen.decision_id)
        assert len(rows) == frozen.observed
        assert {r.status for r in rows} == {"OBSERVATION_FAILED"}
        assert {r.failure_category for r in rows} == {"SETTLEMENT_WINDOW_MISSED"}
        assert all("2026-09-11T15:30:00-04:00" in (r.failure_detail or "") for r in rows)
        events = (
            db_session.query(V4ShadowRunEvent)
            .filter_by(shadow_decision_id=frozen.decision_id, category="SETTLEMENT_WINDOW_MISSED")
            .all()
        )
        assert len(events) == 1
        # Idempotent: a later run finds nothing pending and writes nothing.
        again = settle_due_cohorts(
            db_session, provider=_RaisingProvider(), now=_et(2026, 9, 12, 15, 55)
        )
        assert (again.evaluated, again.failed, again.settled) == (0, 0, 0)
        assert len(_settlements(db_session, frozen.decision_id)) == frozen.observed


class TestJobWiring:
    def test_decision_job_passes_the_v4_predicate_to_orchestration(self):
        """The job body must hand orchestration V4's own window, not V3's."""
        import services.v4_shadow_orchestration as orch
        import services.v4_shadow_scheduler as jobs
        from core.config import Settings, get_settings

        captured = {}

        def recorder(db, settings, **kw):
            captured.update(kw)
            return orch.ShadowRunSummary(0, 0, 0, 0, 0, 0, ())

        enabled = Settings(**{**get_settings().model_dump(), "v4_shadow_enabled": True})
        with (
            patch.object(jobs, "get_settings", return_value=enabled),
            patch("providers.factory.get_options_provider", return_value=_RaisingProvider()),
            patch.object(orch, "run_shadow_decisions_for_due_events", recorder),
        ):
            jobs.run_v4_forward_window_job(now=datetime(2026, 9, 10, 19, 30, tzinfo=UTC))
        assert captured.get("due_predicate") is jobs.due_for_v4_decision_now
