"""V4.2 -- GET /v4/experimental/compatibility. Confirms the router is
live in a non-production app_env (this test suite's own default,
matching every other real test in this file's config) and returns the
exact same result the underlying pure function would."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def test_client() -> Iterator[TestClient]:
    from api.main import app

    with TestClient(app) as client:
        yield client


def test_neutral_long_vol_butterfly_returns_the_real_contradiction(test_client):
    response = test_client.get(
        "/api/v1/v4/experimental/compatibility",
        params={
            "direction": "neutral",
            "volatility_view": "long_vol",
            "strategy": "long_call_butterfly",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["overall_semantic_compatibility"] <= 0.25
    assert "MOVE_INTENT_CONTRADICTION" in body["reason_codes"]
    assert body["tier"] == "contradiction"


def test_missing_volatility_view_is_accepted_and_honestly_underspecified(test_client):
    response = test_client.get(
        "/api/v1/v4/experimental/compatibility",
        params={"direction": "neutral", "strategy": "long_call_butterfly"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["volatility_view"] is None
    assert "MARKET_VIEW_UNDERSPECIFIED" in body["reason_codes"]


def test_unrecognized_strategy_is_rejected_not_silently_guessed(test_client):
    response = test_client.get(
        "/api/v1/v4/experimental/compatibility",
        params={"direction": "neutral", "strategy": "not_a_real_strategy"},
    )
    assert response.status_code == 422


def test_no_mutation_endpoint_exists_under_v4_experimental():
    """Static confirmation over the real router object -- every route
    registered under /v4/experimental is a GET, never a mutation."""
    from api.routers.v4_experimental import router

    assert len(router.routes) > 0
    for route in router.routes:
        assert route.methods == {"GET"}, f"{route.path} exposes a non-GET method"


def test_strike_selection_returns_expected_move_aware_geometry(test_client):
    response = test_client.get(
        "/api/v1/v4/experimental/strike-selection",
        params={
            "strategy": "iron_condor",
            "spot": "100",
            "implied_move_pct": "0.10",
            "strike_spacing": "5",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "constructed"
    assert body["lower_boundary"] == "90"
    assert body["upper_boundary"] == "110"
    assert len(body["legs"]) == 4


def test_strike_selection_without_implied_move_is_unconstructable_where_required(test_client):
    response = test_client.get(
        "/api/v1/v4/experimental/strike-selection",
        params={"strategy": "long_strangle", "spot": "100", "strike_spacing": "5"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "unconstructable"
    assert "UNCONSTRUCTABLE_IMPLIED_MOVE_REQUIRED" in body["reason_codes"]


def test_strike_selection_straddle_needs_no_expected_move_evidence(test_client):
    response = test_client.get(
        "/api/v1/v4/experimental/strike-selection",
        params={"strategy": "long_straddle", "spot": "100", "strike_spacing": "5"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "constructed"


def test_t1_scenario_valuation_returns_full_scenario_grid(test_client):
    response = test_client.get(
        "/api/v1/v4/experimental/t1-scenario-valuation",
        params={
            "strategy": "long_call",
            "right": "call",
            "action": "buy",
            "spot": "100",
            "strike": "100",
            "entry_bid": "4.90",
            "entry_ask": "5.10",
            "entry_iv": "0.60",
            "dte_entry": "3",
            "implied_move_pct": "0.05",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["n_scenarios"] == 21
    assert len(body["scenarios"]) == 21
    assert body["scenario_average_return"] is not None


def test_t1_scenario_valuation_upside_beats_downside_for_long_call(test_client):
    response = test_client.get(
        "/api/v1/v4/experimental/t1-scenario-valuation",
        params={
            "strategy": "long_call",
            "right": "call",
            "action": "buy",
            "spot": "100",
            "strike": "100",
            "entry_bid": "4.90",
            "entry_ask": "5.10",
            "entry_iv": "0.60",
            "dte_entry": "3",
            "implied_move_pct": "0.05",
        },
    )
    body = response.json()
    scenarios = {s["scenario_id"]: s for s in body["scenarios"]}
    upside = scenarios["LARGE_UPSIDE__NORMAL_CRUSH"]["realized_equivalent_pnl_executable"]
    downside = scenarios["LARGE_DOWNSIDE__NORMAL_CRUSH"]["realized_equivalent_pnl_executable"]
    assert float(upside) > float(downside)


def test_t1_scenario_valuation_without_move_evidence_returns_empty_grid(test_client):
    response = test_client.get(
        "/api/v1/v4/experimental/t1-scenario-valuation",
        params={
            "strategy": "long_call",
            "right": "call",
            "action": "buy",
            "spot": "100",
            "strike": "100",
            "entry_bid": "4.90",
            "entry_ask": "5.10",
            "entry_iv": "0.60",
            "dte_entry": "3",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["n_scenarios"] == 0
    assert body["scenarios"] == []
