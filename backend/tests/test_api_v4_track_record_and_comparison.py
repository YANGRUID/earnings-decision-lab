"""Per-configuration track record and same-event comparison
(V4 consolidation, Sections 28-35)."""

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


def _candidate(cid, strategy, legs, ticker):
    ctx = V4T1ValuationContext(
        ticker=ticker, underlying_price=Decimal("100"), observed_at=NOW, entry_timestamp=NOW,
        expected_exit_timestamp=NOW + timedelta(days=1), strategy=strategy,
        expiration=date(2026, 9, 18), legs=tuple(legs), expected_move_context=_em(),
    )
    return ShadowCandidateInput(
        candidate_id=cid, context=ctx,
        leg_retrieved_at={leg.leg_index: NOW for leg in legs},
        external_contract_ids={leg.leg_index: f"conid-{leg.leg_index}" for leg in legs},
    )


def _view():
    return ShadowDecisionView(
        direction="bullish", volatility_view="long_vol", expected_move_intent="large_move",
        confidence="medium", reasoning="r", evidence_refs={}, llm_provider="deepseek",
        llm_model="deepseek-v4-flash", prompt_version="decision-view-v1",
    )


@pytest.fixture
def event(db_session):
    from models.earnings_calendar_event import EarningsCalendarEvent

    row = EarningsCalendarEvent(
        symbol="CMPX", company_name="Compare Co", earnings_date=date(2026, 9, 10),
        earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _freeze(db, event):
    return generate_shadow_decision(
        db, earnings_calendar_event_id=event.id, ticker=event.symbol,
        company_name=event.company_name, legal_decision_window_at=NOW, as_of=NOW,
        view=_view(),
        candidates=[
            _candidate("spread", "bull_call_spread",
                       [_leg(0, "buy", "call", "100", "3.00", "3.20"),
                        _leg(1, "sell", "call", "105", "1.20", "1.40")], event.symbol),
            _candidate("long_put", "long_put",
                       [_leg(0, "buy", "put", "347.50", "10.90", "11.55")], event.symbol),
        ],
        underlying_price=Decimal("100"), underlying_quote_at=NOW,
        market_data_quality="delayed", tws_request_count=4, unique_contracts_quoted=3,
    )


class TestTrackRecordByConfiguration:
    def test_empty_cohort_is_an_honest_empty_state_not_zeros_dressed_as_data(self, client):
        body = client.get("/api/v1/v4/shadow/track-record/by-configuration").json()
        assert len(body["configurations"]) == 6
        for c in body["configurations"]:
            assert c["events"] == 0
            assert c["sample_sufficiency"] == "INSUFFICIENT SAMPLE"
            # Per-cohort observation streams are real counters now; with no
            # evidence they are honestly zero, and outcome metrics stay null
            # below the sample floor.
            assert c["entry_observed"] == 0 and c["settled"] == 0
            assert c["wins"] is None and c["win_rate"] is None
        assert body["sample_floor"] == 30
        assert "no real capital ledger" in body["metrics_note"]

    def test_counts_reflect_frozen_results_per_configuration(self, client, db_session, event):
        _freeze(db_session, event)
        body = client.get("/api/v1/v4/shadow/track-record/by-configuration").json()
        by_key = {c["configuration_key"]: c for c in body["configurations"]}
        assert all(c["events"] == 1 for c in by_key.values())
        # $10K Moderate can rank the long put; every configuration counts
        # exactly once, as RANKED or NO_ACTION, never both.
        for c in by_key.values():
            assert c["actionable"] + c["no_action"] + c["failed"] == 1

    def test_no_portfolio_statistics_are_ever_returned(self, client, db_session, event):
        _freeze(db_session, event)
        body = client.get("/api/v1/v4/shadow/track-record/by-configuration").json()
        text = str(body).lower()
        assert "sharpe" not in text.replace("no portfolio drawdown or sharpe", "")
        assert "drawdown" not in text.replace("no portfolio drawdown or sharpe", "")


