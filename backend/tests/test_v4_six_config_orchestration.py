"""Six configurations wired into the REAL shadow generation path
(V4 consolidation, Sections 3-8, 57-58).

These drive `generate_shadow_decision` -- the authoritative freeze -- on
the isolated test database and assert on the persisted
V4ShadowConfigResult rows, not on the pure evaluation layer (which
tests/test_v4_six_configurations.py already covers).
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from analytics.decision.v4_configurations import V4_CONFIGURATIONS
from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from analytics.decision_timing_policy import V4_ACTIVE_TIMING_POLICY
from models.v4_shadow import V4ShadowCandidate, V4ShadowConfigResult, V4ShadowDecision
from services.v4_shadow import (
    ShadowCandidateInput,
    ShadowDecisionView,
    generate_shadow_decision,
)

NOW = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)
EXPIRATION = date(2026, 9, 18)


@pytest.fixture
def event(db_session):
    from models.earnings_calendar_event import EarningsCalendarEvent

    row = EarningsCalendarEvent(
        symbol="SIXC", company_name="Six Config Co", earnings_date=date(2026, 9, 10),
        earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _em():
    return ExpectedMoveContext(
        spot=Decimal("100"), observed_at=NOW, implied_move_available=True,
        implied_move_dollars=Decimal("5"), implied_move_pct=Decimal("0.05"),
        upper_implied_boundary=Decimal("105"), lower_implied_boundary=Decimal("95"),
        implied_move_source="atm_straddle", implied_move_result=None,
        historical_sample_n=8, historical_evidence_quality="adequate",
        historical_median_abs_move_pct=Decimal("0.04"),
        historical_median_upper_boundary=Decimal("104"),
        historical_median_lower_boundary=Decimal("96"),
        historical_quantiles=None, historical_move_stats=None, context_version="test",
    )


def _leg(i, action, right, strike, bid, ask):
    return V4T1LegInput(
        leg_index=i, action=action, right=right, strike=Decimal(strike), quantity=1,
        multiplier=Decimal("100"), entry_bid=Decimal(bid), entry_ask=Decimal(ask),
        entry_last=None, entry_iv=Decimal("0.40"), entry_delta=None, entry_gamma=None,
        entry_theta=None, entry_vega=None, market_data_quality="delayed",
        external_contract_id=f"conid-{i}",
    )


def _candidate(cid, strategy, legs):
    ctx = V4T1ValuationContext(
        ticker="SIXC", underlying_price=Decimal("100"), observed_at=NOW, entry_timestamp=NOW,
        expected_exit_timestamp=NOW + timedelta(days=1), strategy=strategy,
        expiration=EXPIRATION, legs=tuple(legs), expected_move_context=_em(),
    )
    return ShadowCandidateInput(
        candidate_id=cid, context=ctx,
        leg_retrieved_at={leg.leg_index: NOW for leg in legs},
        external_contract_ids={leg.leg_index: f"conid-{leg.leg_index}" for leg in legs},
    )


def _view():
    return ShadowDecisionView(
        direction="bullish", volatility_view="long_vol", expected_move_intent="large_move",
        confidence="medium", reasoning="synthetic", evidence_refs={},
        llm_provider="deepseek", llm_model="deepseek-v4-flash", prompt_version="decision-view-v1",
    )


def _universe():
    """Cheap spread (fits everywhere), the real PANW-scale long put
    ($1,155 risk), and a wide straddle 2K cannot even afford."""
    return [
        _candidate("spread", "bull_call_spread",
                   [_leg(0, "buy", "call", "100", "3.00", "3.20"),
                    _leg(1, "sell", "call", "105", "1.20", "1.40")]),
        _candidate("long_put", "long_put", [_leg(0, "buy", "put", "347.50", "10.90", "11.55")]),
        _candidate("straddle", "long_straddle",
                   [_leg(0, "buy", "call", "100", "12.00", "12.50"),
                    _leg(1, "buy", "put", "100", "12.00", "12.50")]),
    ]


def _freeze(db, event, candidates=None):
    return generate_shadow_decision(
        db, earnings_calendar_event_id=event.id, ticker="SIXC", company_name="Six Config Co",
        legal_decision_window_at=NOW, as_of=NOW, view=_view(),
        candidates=candidates if candidates is not None else _universe(),
        underlying_price=Decimal("100"), underlying_quote_at=NOW,
        market_data_quality="delayed", tws_request_count=7, unique_contracts_quoted=5,
    )


class TestSixResultsPerFreeze:
    def test_one_freeze_yields_exactly_six_config_results(self, db_session, event):
        """Section 5 -- every event freeze produces all six, never fewer."""
        result = _freeze(db_session, event)
        assert result.status == "RANKED"
        rows = db_session.query(V4ShadowConfigResult).filter_by(
            shadow_decision_id=result.decision_id).all()
        assert sorted(r.configuration_key for r in rows) == sorted(
            c.key for c in V4_CONFIGURATIONS)

    def test_all_six_reference_one_decision_and_one_candidate_universe(self, db_session, event):
        """Section 6 -- common evidence is stored once; the six rows carry
        only configuration-specific differences and point at the shared
        candidate rows by id."""
        result = _freeze(db_session, event)
        assert db_session.query(V4ShadowDecision).count() == 1
        candidate_ids = {
            c.candidate_id for c in db_session.query(V4ShadowCandidate).filter_by(
                shadow_decision_id=result.decision_id)
        }
        for row in db_session.query(V4ShadowConfigResult).all():
            assert row.shadow_decision_id == result.decision_id
            if row.rank_1_candidate_id:
                assert row.rank_1_candidate_id in candidate_ids
            for cid in row.ranked_candidate_ids or []:
                assert cid in candidate_ids

    def test_decision_freezes_the_v4_timing_policy(self, db_session, event):
        result = _freeze(db_session, event)
        decision = db_session.get(V4ShadowDecision, result.decision_id)
        assert decision.decision_timing_policy_version == V4_ACTIVE_TIMING_POLICY.version
        assert "1530" in decision.decision_timing_policy_version

    def test_each_result_persists_its_full_configuration_identity(self, db_session, event):
        """Section 5 -- key, version, capital, profile, counts, rank #1 or
        NO_ACTION reason, exclusion detail, ranking version."""
        _freeze(db_session, event)
        for row in db_session.query(V4ShadowConfigResult).all():
            assert row.configuration_version == "v4-forward-configurations-v1"
            assert row.capital_base in (Decimal("2000.00"), Decimal("10000.00"))
            assert row.risk_profile in {"conservative", "moderate", "aggressive"}
            assert row.ranking_version == "v4-4b-t1-executable-ranking-v1"
            assert row.status in {"RANKED", "NO_ACTION"}
            if row.status == "RANKED":
                assert row.rank_1_candidate_id
            else:
                assert row.no_action_reason


class TestConfigurationsDisagreeHonestly:
    def test_capital_changes_the_answer_for_the_panw_structure(self, db_session, event):
        """Section 8 -- the same evidence yields different, independently
        honest outcomes. The $1,155 long put is excluded at every 2K
        configuration and eligible at 10K Moderate/Aggressive."""
        _freeze(db_session, event)
        by_key = {r.configuration_key: r for r in db_session.query(V4ShadowConfigResult)}

        def excluded_ids(row):
            return {e["candidate_id"]: e["reason_code"] for e in (row.exclusions or [])}

        assert excluded_ids(by_key["v4_2k_moderate"]).get("long_put") == "RISK_CAP_EXCEEDED"
        assert excluded_ids(by_key["v4_2k_aggressive"]).get("straddle") == "CAPITAL_INSUFFICIENT"
        assert "long_put" not in excluded_ids(by_key["v4_10k_moderate"])
        assert "long_put" not in excluded_ids(by_key["v4_10k_aggressive"])

    def test_exclusion_detail_names_the_binding_constraint(self, db_session, event):
        _freeze(db_session, event)
        row = db_session.query(V4ShadowConfigResult).filter_by(
            configuration_key="v4_2k_moderate").one()
        detail = next(e["detail"] for e in row.exclusions if e["candidate_id"] == "long_put")
        assert "$1,155.00" in detail and "$600.00" in detail and "30%" in detail


class TestIdempotency:
    def test_rerun_of_the_same_window_creates_no_duplicate_config_results(
        self, db_session, event
    ):
        """Section 7 -- same event, same timing policy, same engine, same
        configuration version -> still exactly six rows."""
        first = _freeze(db_session, event)
        second = _freeze(db_session, event)
        assert second.status == "ALREADY_GENERATED"
        assert second.decision_id == first.decision_id
        assert db_session.query(V4ShadowConfigResult).count() == 6
        assert db_session.query(V4ShadowDecision).count() == 1


class TestFailureIsolation:
    def test_one_configuration_raising_does_not_lose_the_other_five(
        self, db_session, event, monkeypatch
    ):
        """Section 8 -- a per-configuration failure is persisted as its own
        FAILED row; the remaining five are frozen normally."""
        import services.v4_shadow as shadow_module

        real = shadow_module.evaluate_configuration

        def flaky(candidates, configuration):
            if configuration.key == "v4_10k_aggressive":
                raise RuntimeError("synthetic per-config failure")
            return real(candidates, configuration)

        monkeypatch.setattr(shadow_module, "evaluate_configuration", flaky)
        result = _freeze(db_session, event)
        assert result.status == "RANKED"  # the event-level freeze survived

        rows = {r.configuration_key: r for r in db_session.query(V4ShadowConfigResult)}
        assert len(rows) == 6
        assert rows["v4_10k_aggressive"].status == "FAILED"
        assert "synthetic per-config failure" in (rows["v4_10k_aggressive"].no_action_reason or "")
        assert all(rows[k].status in {"RANKED", "NO_ACTION"}
                   for k in rows if k != "v4_10k_aggressive")


class TestSharedEvidenceAcquiredOnce:
    def test_generation_makes_no_network_calls_for_the_six_configs(
        self, db_session, event, monkeypatch
    ):
        """Section 4 -- six configurations, zero additional LLM/TWS/metadata
        calls. generate_shadow_decision receives already-acquired evidence
        and must not reach out for more; refusing sockets proves it."""
        import socket

        def _refuse(*a, **k):
            raise AssertionError("network I/O during six-config freeze")

        monkeypatch.setattr(socket.socket, "connect", _refuse, raising=False)
        monkeypatch.setattr(socket, "create_connection", _refuse, raising=False)
        result = _freeze(db_session, event)
        assert result.status == "RANKED"
        assert db_session.query(V4ShadowConfigResult).count() == 6

    def test_request_budget_is_recorded_once_at_event_level_not_six_times(
        self, db_session, event
    ):
        """Section 6 -- the TWS request count lives on the decision row.
        No config row carries its own copy of it."""
        result = _freeze(db_session, event)
        decision = db_session.get(V4ShadowDecision, result.decision_id)
        assert decision.tws_request_count == 7
        assert not hasattr(V4ShadowConfigResult, "tws_request_count")


class TestNonVacuous:
    """Section 58 -- the fixtures must genuinely reach the ranked path."""

    def test_at_least_one_configuration_actually_ranked_something(self, db_session, event):
        _freeze(db_session, event)
        ranked = db_session.query(V4ShadowConfigResult).filter_by(status="RANKED").count()
        assert ranked >= 1, "no configuration ranked anything -- fixtures are vacuous"

    def test_at_least_one_configuration_actually_excluded_something(self, db_session, event):
        _freeze(db_session, event)
        assert any(
            (r.excluded_candidate_count or 0) > 0
            for r in db_session.query(V4ShadowConfigResult)
        ), "no configuration excluded anything -- capital/risk assertions would be vacuous"
