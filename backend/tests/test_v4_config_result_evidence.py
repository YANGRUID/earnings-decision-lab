"""V4ShadowConfigResult is append-only evidence (Sections 48-51)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, InternalError, ProgrammingError

from analytics.decision.v4_configurations import (
    V4_CONFIGURATION_VERSION,
    V4_CONFIGURATIONS,
    get_configuration,
)
from analytics.decision_timing_policy import V4_TIMING_POLICY
from models.v4_shadow import SHADOW_SCHEMA_VERSION, V4ShadowConfigResult, V4ShadowDecision


@pytest.fixture
def decision(db_session):
    from models.earnings_calendar_event import EarningsCalendarEvent

    event = EarningsCalendarEvent(
        symbol="CFGX", company_name="Config Co", earnings_date=date(2026, 9, 10),
        earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    row = V4ShadowDecision(
        earnings_calendar_event_id=event.id, ticker="CFGX", company_name="Config Co",
        legal_decision_window_at=datetime(2026, 9, 10, 19, 30, tzinfo=UTC),
        generated_at=datetime(2026, 9, 10, 19, 30, tzinfo=UTC),
        as_of=datetime(2026, 9, 10, 19, 30, tzinfo=UTC),
        status="RANKED", engine_version="v4-test",
        shadow_schema_version=SHADOW_SCHEMA_VERSION,
        decision_timing_policy_version=V4_TIMING_POLICY.version,
    )
    db_session.add(row)
    db_session.flush()
    return row


def _result(decision, key, status="RANKED"):
    config = get_configuration(key)
    return V4ShadowConfigResult(
        shadow_decision_id=decision.id,
        configuration_key=config.key,
        capital_base=config.capital_base,
        risk_profile=config.risk_profile.value,
        configuration_version=V4_CONFIGURATION_VERSION,
        max_risk_dollars=config.max_risk_dollars,
        max_risk_utilization_pct=config.max_risk_utilization_pct,
        status=status,
        eligible_candidate_count=1,
        excluded_candidate_count=0,
    )


class TestSixResultsShareOneEvidenceFreeze:
    def test_all_six_attach_to_a_single_decision(self, db_session, decision):
        for config in V4_CONFIGURATIONS:
            db_session.add(_result(decision, config.key))
        db_session.flush()
        stored = (
            db_session.query(V4ShadowConfigResult)
            .filter_by(shadow_decision_id=decision.id)
            .all()
        )
        assert len(stored) == 6
        # One evidence freeze, six results -- never six decision rows.
        assert db_session.query(V4ShadowDecision).count() == 1

    def test_the_same_configuration_cannot_be_recorded_twice(self, db_session, decision):
        """Section 51 -- exactly one rank #1 per configuration."""
        db_session.add(_result(decision, "v4_2k_moderate"))
        db_session.flush()
        db_session.add(_result(decision, "v4_2k_moderate"))
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestConfigurationIdentityIsPersistedNotInferred:
    def test_capital_and_risk_are_stored_as_their_own_columns(self, db_session, decision):
        """Section 50 -- never re-derived from the key string later."""
        db_session.add(_result(decision, "v4_10k_aggressive"))
        db_session.flush()
        row = db_session.query(V4ShadowConfigResult).one()
        assert row.capital_base == Decimal("10000.00")
        assert row.risk_profile == "aggressive"
        assert row.max_risk_dollars == Decimal("5000.00")
        assert row.configuration_version == V4_CONFIGURATION_VERSION

    def test_decision_freezes_its_timing_policy(self, db_session, decision):
        """Section 23 -- V4 rows must say they ran on the 15:30 clock."""
        assert decision.decision_timing_policy_version == "v4-pre-earnings-1530et-v1"


class TestAppendOnly:
    def test_database_rejects_any_update(self, db_session, decision):
        """The trigger must actually REFUSE, not merely exist. Asserting
        its presence in information_schema would pass even if it were a
        no-op."""
        db_session.add(_result(decision, "v4_2k_conservative"))
        db_session.flush()
        row = db_session.query(V4ShadowConfigResult).one()
        row.status = "NO_ACTION"
        with pytest.raises((InternalError, ProgrammingError)):
            db_session.flush()
        db_session.rollback()

    def test_no_action_is_storable_as_a_first_class_result(self, db_session, decision):
        """Section 17 -- NO_ACTION is evidence, not a failure."""
        result = _result(decision, "v4_2k_conservative", status="NO_ACTION")
        result.no_action_reason = "Risk cap exceeded on every candidate"
        result.eligible_candidate_count = 0
        result.excluded_candidate_count = 3
        db_session.add(result)
        db_session.flush()
        stored = db_session.query(V4ShadowConfigResult).one()
        assert stored.status == "NO_ACTION"
        assert stored.rank_1_candidate_id is None
        assert stored.no_action_reason
