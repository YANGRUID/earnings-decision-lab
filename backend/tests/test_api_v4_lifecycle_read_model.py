"""Per-configuration lifecycle in the six-config read model (Sections 23-27)
and the ticker-scoped decision list."""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from api.deps import get_db
from api.main import app
from models.v4_shadow import V4ShadowObservation, V4ShadowSettlement
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
        spot=Decimal("100"),
        observed_at=NOW,
        implied_move_available=True,
        implied_move_dollars=Decimal("5"),
        implied_move_pct=Decimal("0.05"),
        upper_implied_boundary=Decimal("105"),
        lower_implied_boundary=Decimal("95"),
        implied_move_source="atm_straddle",
        implied_move_result=None,
        historical_sample_n=8,
        historical_evidence_quality="adequate",
        historical_median_abs_move_pct=Decimal("0.04"),
        historical_median_upper_boundary=Decimal("104"),
        historical_median_lower_boundary=Decimal("96"),
        historical_quantiles=None,
        historical_move_stats=None,
        context_version="test",
    )


def _leg(i, action, right, strike, bid, ask):
    return V4T1LegInput(
        leg_index=i,
        action=action,
        right=right,
        strike=Decimal(strike),
        quantity=1,
        multiplier=Decimal("100"),
        entry_bid=Decimal(bid),
        entry_ask=Decimal(ask),
        entry_last=None,
        entry_iv=Decimal("0.40"),
        entry_delta=None,
        entry_gamma=None,
        entry_theta=None,
        entry_vega=None,
        market_data_quality="delayed",
        external_contract_id=f"conid-{i}",
    )


def _candidate(cid, strategy, legs, ticker):
    ctx = V4T1ValuationContext(
        ticker=ticker,
        underlying_price=Decimal("100"),
        observed_at=NOW,
        entry_timestamp=NOW,
        expected_exit_timestamp=NOW + timedelta(days=1),
        strategy=strategy,
        expiration=date(2026, 9, 18),
        legs=tuple(legs),
        expected_move_context=_em(),
    )
    return ShadowCandidateInput(
        candidate_id=cid,
        context=ctx,
        leg_retrieved_at={leg.leg_index: NOW for leg in legs},
        external_contract_ids={leg.leg_index: f"conid-{leg.leg_index}" for leg in legs},
    )


@pytest.fixture
def frozen(db_session):
    from models.earnings_calendar_event import EarningsCalendarEvent

    event = EarningsCalendarEvent(
        symbol="LCX",
        company_name="Lifecycle Co",
        earnings_date=date(2026, 9, 10),
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    result = generate_shadow_decision(
        db_session,
        earnings_calendar_event_id=event.id,
        ticker="LCX",
        company_name="Lifecycle Co",
        legal_decision_window_at=NOW,
        as_of=NOW,
        view=ShadowDecisionView(
            direction="bullish",
            volatility_view="long_vol",
            expected_move_intent="large_move",
            confidence="medium",
            reasoning="r",
            evidence_refs={},
            llm_provider="deepseek",
            llm_model="deepseek-v4-flash",
            prompt_version="decision-view-v1",
        ),
        candidates=[
            _candidate(
                "spread",
                "bull_call_spread",
                [
                    _leg(0, "buy", "call", "100", "3.00", "3.20"),
                    _leg(1, "sell", "call", "105", "1.20", "1.40"),
                ],
                "LCX",
            ),
            _candidate(
                "long_put", "long_put", [_leg(0, "buy", "put", "347.50", "10.90", "11.55")], "LCX"
            ),
        ],
        underlying_price=Decimal("100"),
        underlying_quote_at=NOW,
        market_data_quality="delayed",
        tws_request_count=4,
        unique_contracts_quoted=3,
    )
    assert result.status == "RANKED"
    return result


def _cfgs(client, decision_id):
    body = client.get(f"/api/v1/v4/shadow/decisions/{decision_id}/configurations").json()
    return body, {c["configuration_key"]: c for c in body["configurations"]}


class TestLifecycle:
    def test_before_any_observation_ranked_configs_are_waiting_entry(self, client, frozen):
        body, by_key = _cfgs(client, frozen.decision_id)
        assert body["entry_observation"] is None and body["settlement"] is None
        # The $200-risk spread fits every configuration, so all six are
        # RANKED here; before any observation each must be WAITING_ENTRY,
        # and a NO_ACTION configuration (none in this universe) would keep
        # its own state rather than a lifecycle borrowed from the event.
        for c in by_key.values():
            expected = "WAITING_ENTRY" if c["status"] == "RANKED" else c["status"]
            assert c["lifecycle"] == expected, c
        assert any(c["lifecycle"] == "WAITING_ENTRY" for c in by_key.values())

    def test_entry_observed_only_for_configs_sharing_the_observed_candidate(
        self, client, db_session, frozen
    ):
        db_session.add(
            V4ShadowObservation(
                shadow_decision_id=frozen.decision_id,
                phase="ENTRY",
                candidate_id=frozen.rank_1_candidate_id,
                observed_at=NOW,
                status="OBSERVED",
                market_data_quality="delayed",
                source_provider="ibkr_tws",
            )
        )
        db_session.flush()
        body, by_key = _cfgs(client, frozen.decision_id)
        assert body["entry_observation"]["status"] == "OBSERVED"
        for c in by_key.values():
            if c["status"] != "RANKED":
                continue
            shares = c["rank_1_candidate_id"] == frozen.rank_1_candidate_id
            expected = "WAITING_SETTLEMENT" if shares else "WAITING_ENTRY"
            assert c["lifecycle"] == expected, c["configuration_key"]

    def test_entry_failed_is_its_own_state_not_a_loss(self, client, db_session, frozen):
        db_session.add(
            V4ShadowObservation(
                shadow_decision_id=frozen.decision_id,
                phase="ENTRY",
                candidate_id=frozen.rank_1_candidate_id,
                observed_at=NOW,
                status="NOT_EXECUTABLE",
                failure_category="REQUIRED_SIDE_QUOTE_MISSING",
                failure_detail="no ask on leg 0",
                market_data_quality="delayed",
                source_provider="ibkr_tws",
            )
        )
        db_session.flush()
        body, by_key = _cfgs(client, frozen.decision_id)
        assert body["entry_observation"]["failure_category"] == "REQUIRED_SIDE_QUOTE_MISSING"
        shared = [
            c for c in by_key.values() if c["rank_1_candidate_id"] == frozen.rank_1_candidate_id
        ]
        assert shared and all(c["lifecycle"] == "ENTRY_FAILED" for c in shared)

    def test_settled_state_carries_realized_outcome(self, client, db_session, frozen):
        db_session.add(
            V4ShadowObservation(
                shadow_decision_id=frozen.decision_id,
                phase="ENTRY",
                candidate_id=frozen.rank_1_candidate_id,
                observed_at=NOW,
                status="OBSERVED",
                market_data_quality="delayed",
                source_provider="ibkr_tws",
            )
        )
        db_session.add(
            V4ShadowSettlement(
                shadow_decision_id=frozen.decision_id,
                settled_at=NOW + timedelta(days=1),
                status="SETTLED",
                entry_net_value=Decimal("-180"),
                exit_net_value=Decimal("220"),
                realized_pnl=Decimal("40"),
                return_on_standardized_capital=Decimal("0.02"),
                market_data_quality="delayed",
            )
        )
        db_session.flush()
        body, by_key = _cfgs(client, frozen.decision_id)
        assert body["settlement"]["status"] == "SETTLED"
        assert float(body["settlement"]["realized_pnl"]) == 40.0
        shared = [
            c for c in by_key.values() if c["rank_1_candidate_id"] == frozen.rank_1_candidate_id
        ]
        assert shared and all(c["lifecycle"] == "SETTLED" for c in shared)


class TestTickerScopedList:
    def test_ticker_filter_returns_only_that_company(self, client, frozen):
        body = client.get("/api/v1/v4/shadow/decisions", params={"ticker": "lcx"}).json()
        assert [d["ticker"] for d in body["decisions"]] == ["LCX"]
        _lhs = client.get("/api/v1/v4/shadow/decisions", params={"ticker": "ZZZZ"}).json()[
            "decisions"
        ]
        assert _lhs == []
