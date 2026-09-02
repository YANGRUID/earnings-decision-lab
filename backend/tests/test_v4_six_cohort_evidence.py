"""Six-cohort forward evidence (activation phase, Sections 25-30).

One event whose six configurations diverge onto three different rank #1
candidates; one common evidence acquisition; deduplicated observations;
independent per-configuration entry, settlement, idempotency and failure
isolation. Non-vacuity is asserted explicitly (Section 30).
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import InternalError, ProgrammingError

from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_t1_pricing import T1CandidateDistributionSummary, T1ScenarioResult
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from models.v4_shadow import (
    V4ShadowCandidateObservation,
    V4ShadowConfigEntry,
    V4ShadowConfigResult,
    V4ShadowConfigSettlement,
    V4ShadowDecision,
)
from services.v4_shadow import ShadowCandidateInput, ShadowDecisionView, generate_shadow_decision
from services.v4_shadow_cohort import settle_shadow_decision_cohorts

NOW = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)
EXP = date(2026, 9, 18)


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


def _leg(i, action, right, strike, bid, ask, conid):
    return V4T1LegInput(
        leg_index=i,
        action=action,
        right=right,
        strike=Decimal(strike),
        quantity=1,
        multiplier=Decimal("100"),
        entry_bid=None if bid is None else Decimal(bid),
        entry_ask=None if ask is None else Decimal(ask),
        entry_last=None,
        entry_iv=Decimal("0.40"),
        entry_delta=None,
        entry_gamma=None,
        entry_theta=None,
        entry_vega=None,
        market_data_quality="delayed",
        external_contract_id=conid,
    )


def _candidate(cid, strategy, legs, worst, median):
    ctx = V4T1ValuationContext(
        ticker="SIXC",
        underlying_price=Decimal("100"),
        observed_at=NOW,
        entry_timestamp=NOW,
        expected_exit_timestamp=NOW + timedelta(days=1),
        strategy=strategy,
        expiration=EXP,
        legs=tuple(legs),
        expected_move_context=_em(),
    )
    return ShadowCandidateInput(
        candidate_id=cid,
        context=ctx,
        leg_retrieved_at={leg.leg_index: NOW + timedelta(seconds=leg.leg_index) for leg in legs},
        external_contract_ids={leg.leg_index: leg.external_contract_id for leg in legs},
    )


# The divergent universe (Section 25). Defined risk per contract:
#   A butterfly-like debit spread: $400  -> fits 2K M, 2K A, 10K C/M/A; NOT 2K C ($300)
#   B long put (single-leg long):  $800  -> family-excluded for Conservative; NOT 2K M ($600)
#   C wide vertical:               $3500 -> only 10K A ($5,000)
# Ranking preference among eligible: C > B > A (better downside bands).
def universe(b_ask="8.00"):
    return [
        _candidate(
            "A_butterfly",
            "bull_call_spread",
            [
                _leg(0, "buy", "call", "100", "4.80", "5.00", "c100"),
                _leg(1, "sell", "call", "105", "0.90", "1.00", "c105"),
            ],
            "-0.30",
            "0.04",
        ),
        _candidate(
            "B_put",
            "long_put",
            [_leg(0, "buy", "put", "95", "7.80", b_ask, "p95")],
            "-0.20",
            "0.06",
        ),
        _candidate(
            "C_vertical",
            "bull_call_spread",
            [
                _leg(0, "buy", "call", "110", "39.80", "40.00", "c110w"),
                _leg(1, "sell", "call", "160", "4.90", "5.00", "c160"),
            ],
            "-0.10",
            "0.09",
        ),
    ]


def _view():
    return ShadowDecisionView(
        direction="bullish",
        volatility_view="long_vol",
        expected_move_intent="large_move",
        confidence="medium",
        reasoning="r",
        evidence_refs={},
        llm_provider="deepseek",
        llm_model="deepseek-v4-flash",
        prompt_version="decision-view-v1",
    )


@pytest.fixture
def event(db_session):
    from models.earnings_calendar_event import EarningsCalendarEvent

    row = EarningsCalendarEvent(
        symbol="SIXC",
        company_name="Six Cohort Co",
        earnings_date=date(2026, 9, 10),
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _freeze(db, event, cands=None, monkeypatch=None):
    # Give the ranker real distributions so candidates are RANKABLE and the
    # downside bands order C > B > A (Section 30 non-vacuity).
    import services.v4_shadow as shadow

    real = shadow.evaluate_shadow_candidate

    def with_distribution(candidate):
        rankable, stress = real(candidate)
        worst = {"A_butterfly": "-0.30", "B_put": "-0.20", "C_vertical": "-0.10"}[
            candidate.candidate_id
        ]
        median = {"A_butterfly": "0.04", "B_put": "0.06", "C_vertical": "0.09"}[
            candidate.candidate_id
        ]
        dist = T1CandidateDistributionSummary(
            variant_id="v",
            n_scenarios=7,
            n_valued=7,
            min_return=Decimal(worst),
            max_return=Decimal("0.4"),
            median_return=Decimal(median),
            lower_quartile_return=Decimal(worst),
            positive_scenario_fraction=Decimal("0.6"),
            scenario_average_return=Decimal(median),
            weighted_expected_return=Decimal(median),
            worst_scenario_id="s0",
            worst_scenario_return=Decimal(worst),
            quality_note="synthetic",
        )
        results = tuple(
            T1ScenarioResult(
                variant_id="v",
                scenario_id=f"s{i}",
                underlying_move_label=str(i - 3),
                underlying_move_em_fraction=Decimal(i - 3),
                scenario_underlying_price=Decimal("100"),
                iv_scenario_label="base",
                iv_scenario_multiplier=Decimal("1"),
                dte_remaining_at_exit=7,
                leg_values=(),
                entry_cashflow=Decimal("-100"),
                theoretical_liquidation_value=Decimal("100"),
                executable_liquidation_value=Decimal("100"),
                realized_equivalent_pnl_theoretical=Decimal(median) * 100,
                realized_equivalent_pnl_executable=Decimal(median) * 100,
                return_on_standardized_capital_theoretical=Decimal(median),
                return_on_standardized_capital_executable=Decimal(median),
                return_on_entry_cash=Decimal(median),
                reason_codes=(),
                quality_note="ok",
            )
            for i in range(7)
        )
        import dataclasses

        return dataclasses.replace(rankable, distribution=dist, scenario_results=results), stress

    assert monkeypatch is not None
    monkeypatch.setattr(shadow, "evaluate_shadow_candidate", with_distribution)
    return generate_shadow_decision(
        db,
        earnings_calendar_event_id=event.id,
        ticker="SIXC",
        company_name="Six Cohort Co",
        legal_decision_window_at=NOW,
        as_of=NOW,
        view=_view(),
        candidates=cands if cands is not None else universe(),
        underlying_price=Decimal("100"),
        underlying_quote_at=NOW,
        market_data_quality="delayed",
        tws_request_count=5,
        unique_contracts_quoted=5,
    )


def _cfg_rows(db, decision_id):
    return {
        r.configuration_key: r
        for r in db.query(V4ShadowConfigResult).filter_by(shadow_decision_id=decision_id)
    }


class TestNonVacuous:
    def test_fixtures_produce_three_different_rank_ones_and_one_no_action(
        self, db_session, event, monkeypatch
    ):
        r = _freeze(db_session, event, monkeypatch=monkeypatch)
        assert r.status == "RANKED"
        rows = _cfg_rows(db_session, r.decision_id)
        picks = {k: v.rank_1_candidate_id for k, v in rows.items()}
        assert (
            picks["v4_2k_conservative"] is None and rows["v4_2k_conservative"].status == "NO_ACTION"
        )
        assert picks["v4_2k_moderate"] == "A_butterfly"
        assert picks["v4_2k_aggressive"] == "B_put"
        assert picks["v4_10k_conservative"] == "A_butterfly"
        assert picks["v4_10k_moderate"] == "B_put"
        assert picks["v4_10k_aggressive"] == "C_vertical"
        assert len({p for p in picks.values() if p}) == 3, "fixtures did not diverge"


class TestEntryEvidence:
    def test_one_observation_per_unique_candidate_and_five_config_entries(
        self, db_session, event, monkeypatch
    ):
        r = _freeze(db_session, event, monkeypatch=monkeypatch)
        obs = (
            db_session.query(V4ShadowCandidateObservation)
            .filter_by(shadow_decision_id=r.decision_id, phase="ENTRY")
            .all()
        )
        assert sorted(o.candidate_id for o in obs) == ["A_butterfly", "B_put", "C_vertical"]
        entries = (
            db_session.query(V4ShadowConfigEntry).filter_by(shadow_decision_id=r.decision_id).all()
        )
        assert len(entries) == 5
        assert {e.status for e in entries} == {"OBSERVED"}
        # unique contracts: c100, c105, p95, c100w, c150 = 5 (deduplicated, not 8 legs x configs)
        assert sum(o.unique_contract_count or 0 for o in obs) == 5

    def test_configs_sharing_a_candidate_reference_the_same_observation(
        self, db_session, event, monkeypatch
    ):
        r = _freeze(db_session, event, monkeypatch=monkeypatch)
        entries = {
            e.configuration_key: e
            for e in db_session.query(V4ShadowConfigEntry).filter_by(
                shadow_decision_id=r.decision_id
            )
        }
        assert (
            entries["v4_2k_moderate"].candidate_observation_id
            == entries["v4_10k_conservative"].candidate_observation_id
        )
        assert (
            entries["v4_2k_aggressive"].candidate_observation_id
            == entries["v4_10k_moderate"].candidate_observation_id
        )
        assert entries["v4_10k_aggressive"].candidate_observation_id not in {
            entries["v4_2k_moderate"].candidate_observation_id,
            entries["v4_2k_aggressive"].candidate_observation_id,
        }
        for e in entries.values():
            obs = db_session.get(V4ShadowCandidateObservation, e.candidate_observation_id)
            assert obs.candidate_id == e.candidate_id, (
                "a configuration borrowed another candidate's evidence"
            )

    def test_quantity_and_capital_are_per_configuration(self, db_session, event, monkeypatch):
        r = _freeze(db_session, event, monkeypatch=monkeypatch)
        e = {
            x.configuration_key: x
            for x in db_session.query(V4ShadowConfigEntry).filter_by(
                shadow_decision_id=r.decision_id
            )
        }
        # A: $400 risk, $400 debit. 2K M cap $600 -> 1; 10K C cap $1,500 -> 3.
        assert e["v4_2k_moderate"].quantity == 1 and e["v4_10k_conservative"].quantity == 3
        assert e["v4_10k_conservative"].entry_net_value == e["v4_2k_moderate"].entry_net_value * 3
        assert e["v4_10k_conservative"].standardized_capital == Decimal("10000.00")
        # B: $800 risk. 2K A cap $1,000 -> 1; 10K M cap $3,000 -> 3.
        assert e["v4_2k_aggressive"].quantity == 1 and e["v4_10k_moderate"].quantity == 3
        # C: $3,500 risk; 10K A cap $5,000 -> 1.
        assert e["v4_10k_aggressive"].quantity == 1

    def test_no_network_during_entry_freeze(self, db_session, event, monkeypatch):
        import socket

        def refuse(*a, **k):
            raise AssertionError("network I/O during six-cohort entry freeze")

        monkeypatch.setattr(socket.socket, "connect", refuse, raising=False)
        r = _freeze(db_session, event, monkeypatch=monkeypatch)
        assert (
            db_session.query(V4ShadowConfigEntry)
            .filter_by(shadow_decision_id=r.decision_id)
            .count()
            == 5
        )


class TestPartialEntryFailure:
    def test_missing_ask_on_b_fails_only_b_configs(self, db_session, event, monkeypatch):
        r = _freeze(db_session, event, cands=universe(b_ask=None), monkeypatch=monkeypatch)
        rows = _cfg_rows(db_session, r.decision_id)
        # B is still selected where eligible? A missing ASK makes B unpriceable
        # in the evaluation layer (NOT_PRICEABLE), so those configs fall back
        # to their next eligible candidate or NO_ACTION -- honest either way.
        entries = {
            x.configuration_key: x
            for x in db_session.query(V4ShadowConfigEntry).filter_by(
                shadow_decision_id=r.decision_id
            )
        }
        assert rows["v4_2k_conservative"].status == "NO_ACTION"
        for key in ("v4_2k_moderate", "v4_10k_conservative", "v4_10k_aggressive"):
            assert entries[key].status == "OBSERVED", key
        for e in entries.values():
            assert e.candidate_id != "B_put" or e.status == "NOT_EXECUTABLE"


class TestSettlement:
    class _Provider:
        def __init__(self, missing_bid_for=()):
            self.calls = []
            self.missing = set(missing_bid_for)

        def get_quotes_for_known_contracts(self, ticker, contracts, expiration, observed_at):
            from types import SimpleNamespace

            self.calls.append([c.external_contract_id for c in contracts])
            out = []
            price = {
                "c100": ("6.00", "6.20"),
                "c105": ("1.50", "1.60"),
                "p95": ("9.00", "9.20"),
                "c110w": ("41.00", "41.20"),
                "c160": ("5.50", "5.60"),
            }
            for c in contracts:
                bid, ask = price[c.external_contract_id]
                out.append(
                    SimpleNamespace(
                        strike=c.strike,
                        option_type=c.option_type,
                        bid=None if c.external_contract_id in self.missing else Decimal(bid),
                        ask=Decimal(ask),
                        market_data_quality="delayed",
                        retrieved_at=observed_at,
                    )
                )
            return out

    def test_one_quote_call_covers_all_unique_contracts_and_each_config_gets_its_own_pnl(
        self, db_session, event, monkeypatch
    ):
        r = _freeze(db_session, event, monkeypatch=monkeypatch)
        decision = db_session.get(V4ShadowDecision, r.decision_id)
        provider = self._Provider()
        s = settle_shadow_decision_cohorts(
            db_session, provider=provider, decision=decision, observed_at=NOW + timedelta(days=1)
        )
        assert s.quote_calls == 1 and sorted(provider.calls[0]) == [
            "c100",
            "c105",
            "c110w",
            "c160",
            "p95",
        ]
        assert (
            s.unique_candidates == 3
            and s.unique_contracts == 5
            and s.settled == 5
            and s.failed == 0
        )
        st = {
            x.configuration_key: x
            for x in db_session.query(V4ShadowConfigSettlement).filter_by(
                shadow_decision_id=r.decision_id
            )
        }
        assert set(st) == {
            "v4_2k_moderate",
            "v4_2k_aggressive",
            "v4_10k_conservative",
            "v4_10k_moderate",
            "v4_10k_aggressive",
        }
        # A per unit: entry = buy 5.00 (ASK) - sell 0.90 (BID) = 4.10 -> $410;
        # exit = close long 6.00 (BID) - close short 1.60 (ASK) = 4.40 -> $440; pnl $30/contract
        assert st["v4_2k_moderate"].realized_pnl == Decimal("30.000000")
        assert st["v4_10k_conservative"].realized_pnl == Decimal("90.000000")  # 3 contracts
        assert st["v4_2k_moderate"].return_on_standardized_capital == Decimal("30") / Decimal(
            "2000"
        )
        assert st["v4_10k_conservative"].return_on_standardized_capital == Decimal("90") / Decimal(
            "10000"
        )
        # NO_ACTION config never receives a settlement
        assert "v4_2k_conservative" not in st

    def test_missing_exit_side_fails_only_configs_holding_that_candidate(
        self, db_session, event, monkeypatch
    ):
        r = _freeze(db_session, event, monkeypatch=monkeypatch)
        decision = db_session.get(V4ShadowDecision, r.decision_id)
        s = settle_shadow_decision_cohorts(
            db_session,
            provider=self._Provider(missing_bid_for={"p95"}),
            decision=decision,
            observed_at=NOW + timedelta(days=1),
        )
        st = {
            x.configuration_key: x
            for x in db_session.query(V4ShadowConfigSettlement).filter_by(
                shadow_decision_id=r.decision_id
            )
        }
        assert (
            st["v4_2k_aggressive"].status == "OBSERVATION_FAILED"
            and st["v4_10k_moderate"].status == "OBSERVATION_FAILED"
        )
        assert (
            st["v4_2k_moderate"].status == "SETTLED" and st["v4_10k_aggressive"].status == "SETTLED"
        )
        assert s.settled == 3 and s.failed == 2

    def test_settlement_is_idempotent(self, db_session, event, monkeypatch):
        r = _freeze(db_session, event, monkeypatch=monkeypatch)
        decision = db_session.get(V4ShadowDecision, r.decision_id)
        p = self._Provider()
        settle_shadow_decision_cohorts(
            db_session, provider=p, decision=decision, observed_at=NOW + timedelta(days=1)
        )
        again = settle_shadow_decision_cohorts(
            db_session,
            provider=p,
            decision=decision,
            observed_at=NOW + timedelta(days=1, minutes=5),
        )
        assert (
            again.settled == 0
            and again.failed == 0
            and again.skipped_already == 5
            and again.quote_calls == 0
        )
        assert (
            db_session.query(V4ShadowConfigSettlement)
            .filter_by(shadow_decision_id=r.decision_id)
            .count()
            == 5
        )


class TestEntryIdempotency:
    def test_refreezing_the_same_window_creates_no_duplicate_entries(
        self, db_session, event, monkeypatch
    ):
        r1 = _freeze(db_session, event, monkeypatch=monkeypatch)
        r2 = _freeze(db_session, event, monkeypatch=monkeypatch)
        assert r2.status == "ALREADY_GENERATED" and r2.decision_id == r1.decision_id
        assert (
            db_session.query(V4ShadowConfigEntry)
            .filter_by(shadow_decision_id=r1.decision_id)
            .count()
            == 5
        )
        assert (
            db_session.query(V4ShadowCandidateObservation)
            .filter_by(shadow_decision_id=r1.decision_id)
            .count()
            == 3
        )


class TestImmutability:
    @pytest.mark.parametrize("model", [V4ShadowCandidateObservation, V4ShadowConfigEntry])
    def test_database_rejects_updates(self, db_session, event, monkeypatch, model):
        r = _freeze(db_session, event, monkeypatch=monkeypatch)
        row = db_session.query(model).filter_by(shadow_decision_id=r.decision_id).first()
        row.status = "TAMPERED"
        with pytest.raises((InternalError, ProgrammingError)):
            db_session.flush()
        db_session.rollback()

    def test_settlement_rows_reject_updates(self, db_session, event, monkeypatch):
        r = _freeze(db_session, event, monkeypatch=monkeypatch)
        decision = db_session.get(V4ShadowDecision, r.decision_id)
        settle_shadow_decision_cohorts(
            db_session,
            provider=TestSettlement._Provider(),
            decision=decision,
            observed_at=NOW + timedelta(days=1),
        )
        row = (
            db_session.query(V4ShadowConfigSettlement)
            .filter_by(shadow_decision_id=r.decision_id)
            .first()
        )
        row.realized_pnl = Decimal("999")
        with pytest.raises((InternalError, ProgrammingError)):
            db_session.flush()
        db_session.rollback()
