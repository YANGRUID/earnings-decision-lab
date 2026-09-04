"""A failed settlement must stay exactly as written, and a later recovery
must be appended rather than rewritten -- with the database, not convention,
guaranteeing a configuration is never successfully settled twice.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from analytics.decision.v4_configurations import V4_CONFIGURATION_VERSION, get_configuration
from analytics.decision_timing_policy import V4_TIMING_POLICY
from models.v4_shadow import (
    SHADOW_SCHEMA_VERSION,
    V4ShadowConfigResult,
    V4ShadowConfigSettlement,
    V4ShadowDecision,
)
from services.v4_settlement_history import effective_settlements


@pytest.fixture
def config_result(db_session):
    from models.earnings_calendar_event import EarningsCalendarEvent

    event = EarningsCalendarEvent(
        symbol="SUPX",
        company_name="Supersede Co",
        earnings_date=date(2026, 9, 10),
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    decision = V4ShadowDecision(
        earnings_calendar_event_id=event.id,
        ticker="SUPX",
        company_name="Supersede Co",
        legal_decision_window_at=datetime(2026, 9, 10, 19, 30, tzinfo=UTC),
        generated_at=datetime(2026, 9, 10, 19, 30, tzinfo=UTC),
        as_of=datetime(2026, 9, 10, 19, 30, tzinfo=UTC),
        status="RANKED",
        engine_version="v4-test",
        shadow_schema_version=SHADOW_SCHEMA_VERSION,
        decision_timing_policy_version=V4_TIMING_POLICY.version,
    )
    db_session.add(decision)
    db_session.flush()
    config = get_configuration("v4_2k_moderate")
    result = V4ShadowConfigResult(
        shadow_decision_id=decision.id,
        configuration_key=config.key,
        capital_base=config.capital_base,
        risk_profile=config.risk_profile.value,
        configuration_version=V4_CONFIGURATION_VERSION,
        max_risk_dollars=config.max_risk_dollars,
        max_risk_utilization_pct=config.max_risk_utilization_pct,
        status="RANKED",
        eligible_candidate_count=1,
        excluded_candidate_count=0,
    )
    db_session.add(result)
    db_session.flush()
    return result


def _row(row_id, result_id, status):
    return SimpleNamespace(id=row_id, shadow_config_result_id=result_id, status=status)


class TestEffectiveSettlements:
    def test_a_later_attempt_supersedes_an_earlier_one(self):
        rows = [_row(1, 10, "OBSERVATION_FAILED"), _row(2, 10, "SETTLED")]
        out = effective_settlements(rows)
        assert [r.id for r in out] == [2]

    def test_input_order_does_not_decide_the_winner(self):
        rows = [_row(2, 10, "SETTLED"), _row(1, 10, "OBSERVATION_FAILED")]
        assert [r.id for r in effective_settlements(rows)] == [2]

    def test_configurations_are_kept_separate(self):
        rows = [
            _row(1, 10, "OBSERVATION_FAILED"),
            _row(2, 10, "SETTLED"),
            _row(3, 11, "SETTLED"),
        ]
        assert {r.shadow_config_result_id for r in effective_settlements(rows)} == {10, 11}

    def test_a_configuration_counts_once_not_twice(self):
        """The double-count this helper exists to prevent: one failed row and
        one settled row for the SAME configuration is one position."""
        rows = [_row(1, 10, "OBSERVATION_FAILED"), _row(2, 10, "SETTLED")]
        out = effective_settlements(rows)
        assert len(out) == 1
        assert sum(1 for r in out if r.status == "SETTLED") == 1
        assert sum(1 for r in out if r.status != "SETTLED") == 0

    def test_an_unrecovered_failure_stays_failed(self):
        assert [r.status for r in effective_settlements([_row(1, 10, "OBSERVATION_FAILED")])] == [
            "OBSERVATION_FAILED"
        ]

    def test_no_rows_is_no_settlement(self):
        assert effective_settlements([]) == []


class TestDatabaseInvariants:
    def _settlement(self, result, status, **kw):
        fields = {
            "shadow_config_result_id": result.id,
            "shadow_decision_id": result.shadow_decision_id,
            "configuration_key": result.configuration_key,
            "candidate_id": "long_call:test",
            "status": status,
            "quantity": 1,
            "standardized_capital": Decimal("2000"),
            "settled_at": datetime(2026, 9, 11, 19, 30, tzinfo=UTC),
            "pricing_convention": "V4_EXIT_EOD_FALLBACK",
        }
        fields.update(kw)
        return V4ShadowConfigSettlement(**fields)

    def test_a_failed_attempt_and_a_recovery_can_coexist(self, db_session, config_result):
        failed = self._settlement(config_result, "OBSERVATION_FAILED")
        db_session.add(failed)
        db_session.flush()
        db_session.add(
            self._settlement(
                config_result,
                "SETTLED",
                realized_pnl=Decimal("12.50"),
                pricing_method="MARKET_CLOSE_FALLBACK",
                recovery_provenance="LATE_SETTLEMENT_OVERRIDE",
                supersedes_settlement_id=failed.id,
            )
        )
        db_session.flush()
        rows = (
            db_session.query(V4ShadowConfigSettlement)
            .filter_by(shadow_config_result_id=config_result.id)
            .all()
        )
        assert len(rows) == 2
        assert effective_settlements(rows)[0].status == "SETTLED"

    def test_a_configuration_can_never_be_settled_twice(self, db_session, config_result):
        db_session.add(self._settlement(config_result, "SETTLED"))
        db_session.flush()
        db_session.add(self._settlement(config_result, "SETTLED"))
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_a_written_settlement_can_never_be_rewritten(self, db_session, config_result):
        """The original failure evidence is immutable -- recovery appends,
        it does not edit."""
        row = self._settlement(config_result, "OBSERVATION_FAILED")
        db_session.add(row)
        db_session.flush()
        row.status = "SETTLED"
        with pytest.raises(Exception):  # noqa: B017 -- DB trigger, driver-specific type
            db_session.flush()
