"""V4.2 -- volume / open interest / size capture must stay honest.

The gap being closed: the provider requests generic ticks 100/101/106, which
supply volume and open interest, but V4ShadowCandidateLeg was constructed
without them -- 0 of 211 persisted legs carried either. These pin the one
property that matters once they ARE persisted: a real zero and a missing
value must never become the same thing.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from analytics.decision.v4_2_earnings_friction import (
    FrictionObservation,
    build_earnings_friction_cohort,
)
from models.v4_shadow import (
    SHADOW_SCHEMA_VERSION,
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowDecision,
)
from models.earnings_calendar_event import EarningsCalendarEvent
from analytics.decision_timing_policy import V4_TIMING_POLICY

D = Decimal


@pytest.fixture
def candidate(db_session):
    event = EarningsCalendarEvent(
        symbol="LIQX", company_name="Liquidity Co", earnings_date=date(2026, 9, 3),
        earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    decision = V4ShadowDecision(
        earnings_calendar_event_id=event.id, ticker="LIQX", company_name="Liquidity Co",
        legal_decision_window_at=datetime(2026, 9, 3, 19, 30, tzinfo=UTC),
        generated_at=datetime(2026, 9, 3, 19, 30, tzinfo=UTC),
        as_of=datetime(2026, 9, 3, 19, 30, tzinfo=UTC),
        status="RANKED", engine_version="v4-test",
        shadow_schema_version=SHADOW_SCHEMA_VERSION,
        decision_timing_policy_version=V4_TIMING_POLICY.version,
        candidate_count=1, rankable_candidate_count=1, underlying_price=D("100"),
    )
    db_session.add(decision)
    db_session.flush()
    row = V4ShadowCandidate(
        shadow_decision_id=decision.id, candidate_id="c:v1", strategy="long_call",
        expiration=date(2026, 9, 18), validity_status="RANKABLE",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _leg(candidate, index, **kwargs):
    return V4ShadowCandidateLeg(
        shadow_candidate_id=candidate.id, leg_index=index, action="buy", right="call",
        strike=D("100"), quantity=1, multiplier=D("100"), **kwargs
    )


class TestZeroIsNotMissing:
    def test_a_real_zero_persists_as_zero(self, db_session, candidate):
        """An option with genuinely no open interest is evidence. Turning it
        into NULL would lose that."""
        db_session.add(_leg(candidate, 0, volume=0, open_interest=0,
                            bid_size=0, ask_size=0))
        db_session.flush()
        row = db_session.query(V4ShadowCandidateLeg).filter_by(leg_index=0).one()
        assert row.volume == 0
        assert row.open_interest == 0
        assert row.bid_size == 0
        assert row.ask_size == 0

    def test_a_missing_value_persists_as_null_not_zero(self, db_session, candidate):
        """The provider not supplying a figure is a different fact from the
        market reporting none."""
        db_session.add(_leg(candidate, 1))
        db_session.flush()
        row = db_session.query(V4ShadowCandidateLeg).filter_by(leg_index=1).one()
        assert row.volume is None
        assert row.open_interest is None
        assert row.bid_size is None
        assert row.ask_size is None

    def test_the_two_remain_distinguishable_in_a_query(self, db_session, candidate):
        db_session.add(_leg(candidate, 2, open_interest=0))
        db_session.add(_leg(candidate, 3))
        db_session.flush()
        rows = db_session.query(V4ShadowCandidateLeg).filter(
            V4ShadowCandidateLeg.shadow_candidate_id == candidate.id
        ).all()
        reported_zero = [r for r in rows if r.open_interest == 0]
        not_provided = [r for r in rows if r.open_interest is None]
        assert len(reported_zero) == 1
        assert len(not_provided) == 1

    def test_market_data_quality_travels_with_the_liquidity_evidence(self, db_session, candidate):
        db_session.add(_leg(candidate, 4, volume=12, market_data_quality="delayed"))
        db_session.flush()
        row = db_session.query(V4ShadowCandidateLeg).filter_by(leg_index=4).one()
        assert row.market_data_quality == "delayed", "delayed must never read as live"


class TestFrictionCohortHandlesAbsence:
    def test_missing_volume_and_oi_do_not_become_zero(self):
        observation = FrictionObservation(
            relative_spread=D("0.10"), absolute_spread=D("0.05"), dte=1, moneyness=D("1.0")
        )
        assert observation.volume is None
        assert observation.open_interest is None

    def test_a_cohort_built_without_them_still_reports_spreads(self):
        cohort = build_earnings_friction_cohort(
            [FrictionObservation(relative_spread=D(s), absolute_spread=D("0.05"),
                                 dte=1, moneyness=D("1.0"))
             for s in ("0.04", "0.08", "0.12", "0.16")],
            distinct_events=4,
        )
        assert cohort.p50_relative_spread == D("0.10")
        assert cohort.observations == 4
