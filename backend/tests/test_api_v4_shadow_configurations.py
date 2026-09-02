"""Six-configuration read model (V4 consolidation, Sections 53-54)."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from api.deps import get_db
from api.main import app
from services.v4_shadow import ShadowCandidateInput, ShadowDecisionView, generate_shadow_decision

NOW = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)


@pytest.fixture
def client(db_session):
    app.dependency_overrides[get_db] = lambda: db_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_db, None)


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
        ticker="RMX", underlying_price=Decimal("100"), observed_at=NOW, entry_timestamp=NOW,
        expected_exit_timestamp=NOW + timedelta(days=1), strategy=strategy,
        expiration=date(2026, 9, 18), legs=tuple(legs), expected_move_context=_em(),
    )
    return ShadowCandidateInput(
        candidate_id=cid, context=ctx,
        leg_retrieved_at={leg.leg_index: NOW for leg in legs},
        external_contract_ids={leg.leg_index: f"conid-{leg.leg_index}" for leg in legs},
    )


@pytest.fixture
def frozen_decision(db_session):
    from models.earnings_calendar_event import EarningsCalendarEvent

    event = EarningsCalendarEvent(
        symbol="RMX", company_name="Read Model Co", earnings_date=date(2026, 9, 10),
        earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    result = generate_shadow_decision(
        db_session, earnings_calendar_event_id=event.id, ticker="RMX",
        company_name="Read Model Co", legal_decision_window_at=NOW, as_of=NOW,
        view=ShadowDecisionView(
            direction="bullish", volatility_view="long_vol", expected_move_intent="large_move",
            confidence="medium", reasoning="r", evidence_refs={}, llm_provider="deepseek",
            llm_model="deepseek-v4-flash", prompt_version="decision-view-v1",
        ),
        candidates=[
            _candidate("spread", "bull_call_spread",
                       [_leg(0, "buy", "call", "100", "3.00", "3.20"),
                        _leg(1, "sell", "call", "105", "1.20", "1.40")]),
            _candidate("long_put", "long_put", [_leg(0, "buy", "put", "347.50", "10.90", "11.55")]),
        ],
        underlying_price=Decimal("100"), underlying_quote_at=NOW,
        market_data_quality="delayed", tws_request_count=4, unique_contracts_quoted=3,
    )
    assert result.status == "RANKED"
    return result


class TestSixConfigReadModel:
    def test_one_round_trip_returns_evidence_six_configs_and_candidates(
        self, client, frozen_decision
    ):
        r = client.get(f"/api/v1/v4/shadow/decisions/{frozen_decision.decision_id}/configurations")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["decision"]["id"] == frozen_decision.decision_id
        assert len(body["configurations"]) == 6
        assert [c["configuration_key"] for c in body["configurations"]] == [
            "v4_2k_conservative", "v4_2k_moderate", "v4_2k_aggressive",
            "v4_10k_conservative", "v4_10k_moderate", "v4_10k_aggressive",
        ]
        assert len(body["candidates"]) == 2
        assert body["default_configuration_key"] == "v4_2k_moderate"
        assert body["timing_policy_version"] == "v4-1530-entry-1530-t1-settlement-v2"  # active v2

    def test_each_configuration_carries_its_own_identity_and_rank_1(self, client, frozen_decision):
        body = client.get(
            f"/api/v1/v4/shadow/decisions/{frozen_decision.decision_id}/configurations"
        ).json()
        by_key = {c["configuration_key"]: c for c in body["configurations"]}
        assert by_key["v4_10k_moderate"]["capital_base"] == "10000.00"
        assert by_key["v4_10k_moderate"]["risk_profile"] == "moderate"
        assert by_key["v4_10k_moderate"]["max_risk_dollars"] == "3000.00"
        ranked = [c for c in body["configurations"] if c["status"] == "RANKED"]
        assert ranked and all(c["rank_1"] is not None for c in ranked)

    def test_exclusion_reasons_are_present_for_the_ui(self, client, frozen_decision):
        body = client.get(
            f"/api/v1/v4/shadow/decisions/{frozen_decision.decision_id}/configurations"
        ).json()
        two_k_mod = next(
            c for c in body["configurations"] if c["configuration_key"] == "v4_2k_moderate"
        )
        codes = {e["candidate_id"]: e["reason_code"] for e in two_k_mod["exclusions"]}
        assert codes.get("long_put") == "RISK_CAP_EXCEEDED"

    def test_unknown_decision_is_404(self, client):
        assert client.get("/api/v1/v4/shadow/decisions/999999/configurations").status_code == 404

    def test_response_is_labelled_experimental(self, client, frozen_decision):
        body = client.get(
            f"/api/v1/v4/shadow/decisions/{frozen_decision.decision_id}/configurations"
        ).json()
        assert "EXPERIMENTAL" in body["notice"].upper()
