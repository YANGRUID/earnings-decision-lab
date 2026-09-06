"""V4.2 -- challenger ENTRY evidence.

What must hold before a challenger position is allowed to exist: it is priced
on the executable side, it is quoted once no matter how many configurations
hold it, a configuration that declined gets nothing at all, and none of it can
be rewritten afterwards.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy.exc import DatabaseError, IntegrityError

from analytics.decision.v4_2_viability import VIABILITY_GATE_VERSION
from analytics.decision_timing_policy import V4_TIMING_POLICY
from models.earnings_calendar_event import EarningsCalendarEvent
from models.v4_2_challenger import (
    V42ChallengerCandidateObservation,
    V42ChallengerConfigEntry,
    V42ChallengerConfigResult,
    V42ChallengerDecision,
)
from models.v4_shadow import (
    SHADOW_SCHEMA_VERSION,
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowDecision,
)
from services.v4_2_challenger_entry import (
    ENTRY_STATUS_NOT_EXECUTABLE,
    freeze_challenger_entries,
    max_defined_risk_from_legs,
)

D = Decimal
OBSERVED = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)
SETTLEMENT_DATE = date(2026, 9, 11)


def _build(db, *, symbol, legs, expiration=date(2026, 9, 25)):
    """A control decision plus a frozen challenger decision over it.

    ``legs`` is a list of (action, right, strike, conid, bid, ask): the exact
    frozen quotes the challenger's entry must price against.
    """
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Co",
        earnings_date=date(2026, 9, 10),
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db.add(event)
    db.flush()
    control = V4ShadowDecision(
        earnings_calendar_event_id=event.id,
        ticker=symbol,
        company_name=f"{symbol} Co",
        legal_decision_window_at=OBSERVED,
        generated_at=OBSERVED,
        as_of=OBSERVED,
        status="RANKED",
        engine_version="v4-test",
        shadow_schema_version=SHADOW_SCHEMA_VERSION,
        decision_timing_policy_version=V4_TIMING_POLICY.version,
        candidate_count=1,
        rankable_candidate_count=1,
        underlying_price=D("100"),
        market_data_quality="delayed",
    )
    db.add(control)
    db.flush()
    candidate = V4ShadowCandidate(
        shadow_decision_id=control.id,
        candidate_id="spread:v1",
        strategy="bull_call_spread",
        expiration=expiration,
        validity_status="RANKABLE",
        core_median_return=D("0.08"),
        entry_cash_required=D("200"),
    )
    db.add(candidate)
    db.flush()
    for index, (action, right, strike, conid, bid, ask) in enumerate(legs):
        db.add(
            V4ShadowCandidateLeg(
                shadow_candidate_id=candidate.id,
                leg_index=index,
                action=action,
                right=right,
                strike=D(strike),
                quantity=1,
                multiplier=D("100"),
                external_contract_id=conid,
                bid=None if bid is None else D(bid),
                ask=None if ask is None else D(ask),
                bid_size=4,
                ask_size=7,
                volume=120,
                open_interest=900,
                market_data_quality="delayed",
            )
        )
    challenger = V42ChallengerDecision(
        earnings_calendar_event_id=event.id,
        shadow_decision_id=control.id,
        ticker=symbol,
        generated_at=OBSERVED,
        observed_at=OBSERVED,
        gate_version=VIABILITY_GATE_VERSION,
        move_edge_version="test",
        status="RANKED",
        selected_candidate_id="spread:v1",
        candidates_evaluated=1,
        candidates_accepted=1,
    )
    db.add(challenger)
    db.flush()
    return challenger


def _configs(db, challenger, *, keys, status="RANKED", candidate="spread:v1"):
    for key in keys:
        db.add(
            V42ChallengerConfigResult(
                challenger_decision_id=challenger.id,
                configuration_key=key,
                capital_base=D("2000"),
                risk_profile="moderate",
                max_risk_dollars=D("400"),
                status=status,
                selected_candidate_id=candidate if status == "RANKED" else None,
                no_action_reason=None if status == "RANKED" else "no candidate cleared the gate",
            )
        )
    db.flush()


@pytest.fixture
def six_configs(db_session):
    challenger = _build(
        db_session,
        symbol="ENTA",
        legs=[
            ("buy", "call", "100", "111", "1.00", "1.10"),
            ("sell", "call", "105", "222", "0.40", "0.50"),
        ],
    )
    _configs(
        db_session,
        challenger,
        keys=[
            "v4_2k_conservative",
            "v4_2k_moderate",
            "v4_2k_aggressive",
            "v4_10k_conservative",
            "v4_10k_moderate",
            "v4_10k_aggressive",
        ],
    )
    return challenger


class TestExecutableSide:
    def test_a_long_leg_is_opened_at_the_ask(self, db_session):
        challenger = _build(
            db_session,
            symbol="LONGA",
            legs=[("buy", "call", "100", "111", "1.00", "1.30")],
        )
        _configs(db_session, challenger, keys=["v4_2k_moderate"])
        freeze_challenger_entries(db_session, challenger=challenger)
        obs = db_session.query(V42ChallengerCandidateObservation).one()
        # Paying the ASK on one long contract: 1.30 * 1 * 100.
        assert obs.net_executable_value == D("130")
        leg = obs.legs_json["legs"][0]
        assert leg["required_side"] == "ask"
        assert leg["pricing_source"] == "EXECUTABLE_ASK"

    def test_a_short_leg_is_opened_at_the_bid(self, db_session):
        challenger = _build(
            db_session,
            symbol="SHRTA",
            legs=[("sell", "put", "95", "333", "2.00", "2.60")],
        )
        _configs(db_session, challenger, keys=["v4_2k_moderate"])
        freeze_challenger_entries(db_session, challenger=challenger)
        obs = db_session.query(V42ChallengerCandidateObservation).one()
        # Receiving the BID on one short contract: -(2.00 * 1 * 100).
        assert obs.net_executable_value == D("-200")
        leg = obs.legs_json["legs"][0]
        assert leg["required_side"] == "bid"
        assert leg["pricing_source"] == "EXECUTABLE_BID"

    def test_no_midpoint_or_last_is_ever_substituted(self, db_session):
        """A long leg with no ASK is NOT priced from its bid, its midpoint or
        anything else -- the observation fails."""
        challenger = _build(
            db_session,
            symbol="MISSA",
            legs=[("buy", "call", "100", "111", "1.00", None)],
        )
        _configs(db_session, challenger, keys=["v4_2k_moderate"])
        summary = freeze_challenger_entries(db_session, challenger=challenger)
        obs = db_session.query(V42ChallengerCandidateObservation).one()
        assert obs.status == ENTRY_STATUS_NOT_EXECUTABLE
        assert obs.net_executable_value is None
        assert summary.entries_observed == 0
        assert summary.entries_failed == 1
        assert "no midpoint" in obs.failure_detail


class TestOneQuoteManyConfigurations:
    def test_six_configurations_share_one_observation(self, db_session, six_configs):
        summary = freeze_challenger_entries(db_session, challenger=six_configs)
        observations = db_session.query(V42ChallengerCandidateObservation).all()
        entries = db_session.query(V42ChallengerConfigEntry).all()
        assert len(observations) == 1, "one candidate must be quoted once, not six times"
        assert len(entries) == 6
        assert {e.candidate_observation_id for e in entries} == {observations[0].id}
        assert summary.entries_observed == 6

    def test_the_challenger_issues_no_market_data_request_at_entry(self, db_session, six_configs):
        summary = freeze_challenger_entries(db_session, challenger=six_configs)
        assert summary.market_data_requests_issued == 0
        assert summary.contracts_shared_with_control == summary.unique_contracts == 2

    def test_each_configuration_sizes_itself_independently(self, db_session, six_configs):
        freeze_challenger_entries(db_session, challenger=six_configs)
        entries = {e.configuration_key: e for e in db_session.query(V42ChallengerConfigEntry)}
        assert len(entries) == 6
        # Every entry must carry its OWN quantity and capital, and the entry
        # value must be that quantity times the shared per-unit price.
        obs = db_session.query(V42ChallengerCandidateObservation).one()
        for entry in entries.values():
            assert entry.quantity >= 1
            assert entry.entry_net_value == obs.net_executable_value * entry.quantity

    def test_different_candidates_get_their_own_observations(self, db_session):
        challenger = _build(
            db_session,
            symbol="SPLIT",
            legs=[("buy", "call", "100", "111", "1.00", "1.10")],
        )
        # A second candidate the control also froze, selected by one config.
        control_id = challenger.shadow_decision_id
        other = V4ShadowCandidate(
            shadow_decision_id=control_id,
            candidate_id="other:v1",
            strategy="long_put",
            expiration=date(2026, 9, 25),
            validity_status="RANKABLE",
            core_median_return=D("0.02"),
            entry_cash_required=D("150"),
        )
        db_session.add(other)
        db_session.flush()
        db_session.add(
            V4ShadowCandidateLeg(
                shadow_candidate_id=other.id,
                leg_index=0,
                action="buy",
                right="put",
                strike=D("95"),
                quantity=1,
                multiplier=D("100"),
                external_contract_id="999",
                bid=D("0.80"),
                ask=D("0.95"),
            )
        )
        db_session.flush()
        _configs(db_session, challenger, keys=["v4_2k_moderate", "v4_10k_moderate"])
        db_session.add(
            V42ChallengerConfigResult(
                challenger_decision_id=challenger.id,
                configuration_key="v4_2k_aggressive",
                capital_base=D("2000"),
                risk_profile="aggressive",
                max_risk_dollars=D("600"),
                status="RANKED",
                selected_candidate_id="other:v1",
            )
        )
        db_session.flush()
        summary = freeze_challenger_entries(db_session, challenger=challenger)
        observations = db_session.query(V42ChallengerCandidateObservation).all()
        assert len(observations) == 2
        # Three contracts across two candidates, each counted once.
        assert summary.unique_contracts == 2
        assert summary.entries_observed == 3


class TestNoAction:
    def test_a_declined_configuration_gets_no_entry_at_all(self, db_session):
        challenger = _build(
            db_session,
            symbol="NOACT",
            legs=[("buy", "call", "100", "111", "1.00", "1.10")],
        )
        _configs(db_session, challenger, keys=["v4_2k_moderate"], status="NO_ACTION")
        summary = freeze_challenger_entries(db_session, challenger=challenger)
        assert db_session.query(V42ChallengerConfigEntry).count() == 0
        assert db_session.query(V42ChallengerCandidateObservation).count() == 0
        assert summary.no_action == 1
        assert summary.status == "NO_ACTION"

    def test_a_mixed_decision_freezes_only_the_actioning_configurations(self, db_session):
        challenger = _build(
            db_session,
            symbol="MIXED",
            legs=[("buy", "call", "100", "111", "1.00", "1.10")],
        )
        _configs(db_session, challenger, keys=["v4_2k_moderate"])
        _configs(db_session, challenger, keys=["v4_2k_conservative"], status="NO_ACTION")
        freeze_challenger_entries(db_session, challenger=challenger)
        entries = db_session.query(V42ChallengerConfigEntry).all()
        assert [e.configuration_key for e in entries] == ["v4_2k_moderate"]


class TestIdempotencyAndImmutability:
    def test_a_second_freeze_is_a_no_op(self, db_session, six_configs):
        freeze_challenger_entries(db_session, challenger=six_configs)
        again = freeze_challenger_entries(db_session, challenger=six_configs)
        assert db_session.query(V42ChallengerConfigEntry).count() == 6
        assert again.entries_observed == 0
        assert again.already_frozen == 6

    def test_one_entry_of_record_per_configuration(self, db_session, six_configs):
        freeze_challenger_entries(db_session, challenger=six_configs)
        db_session.flush()
        first = db_session.query(V42ChallengerConfigEntry).first()
        duplicate = V42ChallengerConfigEntry(
            challenger_config_result_id=first.challenger_config_result_id,
            challenger_decision_id=first.challenger_decision_id,
            candidate_observation_id=first.candidate_observation_id,
            configuration_key=first.configuration_key,
            candidate_id=first.candidate_id,
            status="OBSERVED",
            quantity=1,
            standardized_capital=D("2000"),
            pricing_convention="BUY_AT_ASK_SELL_AT_BID",
            observed_at=OBSERVED,
        )
        db_session.add(duplicate)
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_an_entry_observation_cannot_be_updated(self, db_session, six_configs):
        freeze_challenger_entries(db_session, challenger=six_configs)
        db_session.commit()
        obs = db_session.query(V42ChallengerCandidateObservation).first()
        obs.net_executable_value = D("999")
        with pytest.raises(DatabaseError):
            db_session.flush()
        db_session.rollback()

    def test_a_frozen_entry_cannot_be_updated(self, db_session, six_configs):
        freeze_challenger_entries(db_session, challenger=six_configs)
        db_session.commit()
        entry = db_session.query(V42ChallengerConfigEntry).first()
        entry.quantity = 99
        with pytest.raises(DatabaseError):
            db_session.flush()
        db_session.rollback()


class TestFrozenPosition:
    def test_the_exact_contracts_are_frozen_on_the_entry(self, db_session, six_configs):
        freeze_challenger_entries(
            db_session, challenger=six_configs, settlement_date=SETTLEMENT_DATE
        )
        entry = db_session.query(V42ChallengerConfigEntry).first()
        legs = entry.frozen_legs_json["legs"]
        assert [leg["external_contract_id"] for leg in legs] == ["111", "222"]
        assert entry.expiration == date(2026, 9, 25)
        assert entry.dte_at_settlement == 14
        assert entry.entry_dte == 15

    def test_liquidity_evidence_is_carried_verbatim(self, db_session, six_configs):
        freeze_challenger_entries(db_session, challenger=six_configs)
        leg = db_session.query(V42ChallengerCandidateObservation).one().legs_json["legs"][0]
        assert leg["bid_size"] == 4
        assert leg["ask_size"] == 7
        assert leg["volume"] == 120
        assert leg["open_interest"] == 900
        assert leg["market_data_quality"] == "delayed"


class TestRiskSizing:
    def test_max_defined_risk_matches_the_controls_definition(self, db_session, six_configs):
        legs = db_session.query(V4ShadowCandidateLeg).order_by(V4ShadowCandidateLeg.leg_index).all()
        # Long 100 call at 1.10, short 105 call at 0.40: net debit 0.70, and a
        # vertical's max loss is exactly the debit paid.
        assert max_defined_risk_from_legs(legs) == D("70.000")

    def test_an_unpriceable_leg_yields_no_risk_number(self, db_session):
        challenger = _build(
            db_session,
            symbol="NORISK",
            legs=[("buy", "call", "100", "111", "1.00", None)],
        )
        legs = db_session.query(V4ShadowCandidateLeg).all()
        assert max_defined_risk_from_legs(legs) is None
        assert challenger is not None
