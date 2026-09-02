"""V4.4C -- shadow decision/candidate freeze and observation lifecycle
(Sections 57, 58).

Uses synthetic point-in-time data against the isolated test database. No
live IBKR socket is opened (conftest's guard would refuse one), and no
official V3 row is touched.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from models.v4_shadow import (
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowDecision,
    V4ShadowObservation,
)
from services.v4_shadow import (
    DECISION_VIEW_SCHEMA_VERSION,
    ShadowCandidateInput,
    ShadowDecisionView,
    generate_shadow_decision,
    record_observation,
)

WINDOW = datetime(2026, 9, 10, 19, 55, tzinfo=UTC)
EXPIRATION = date(2026, 9, 18)


@pytest.fixture
def calendar_event(db_session):
    """A real earnings_calendar_event row -- shadow decisions key on the
    authoritative event id (Section 46), so one must exist."""
    from models.earnings_calendar_event import EarningsCalendarEvent

    event = EarningsCalendarEvent(
        symbol="TSTX",
        company_name="Test Co",
        earnings_date=date(2026, 9, 10),
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    return event


def _em_context() -> ExpectedMoveContext:
    return ExpectedMoveContext(
        spot=Decimal("100"),
        observed_at=WINDOW,
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


def _leg(index, action, right, strike, bid="2.00", ask="2.20", iv="0.40"):
    return V4T1LegInput(
        leg_index=index,
        action=action,
        right=right,
        strike=Decimal(strike),
        quantity=1,
        multiplier=Decimal("100"),
        entry_bid=Decimal(bid) if bid else None,
        entry_ask=Decimal(ask) if ask else None,
        entry_last=None,
        entry_iv=Decimal(iv) if iv else None,
        entry_delta=None,
        entry_gamma=None,
        entry_theta=None,
        entry_vega=None,
        market_data_quality="delayed",
        external_contract_id=f"c{index}",
    )


def _candidate(candidate_id, strategy, legs, skew_seconds=0):
    context = V4T1ValuationContext(
        ticker="TSTX",
        underlying_price=Decimal("100"),
        observed_at=WINDOW,
        entry_timestamp=WINDOW,
        expected_exit_timestamp=WINDOW + timedelta(days=1),
        strategy=strategy,
        expiration=EXPIRATION,
        legs=legs,
        expected_move_context=_em_context(),
    )
    return ShadowCandidateInput(
        candidate_id=candidate_id,
        context=context,
        leg_retrieved_at={
            leg.leg_index: WINDOW + timedelta(seconds=i * skew_seconds)
            for i, leg in enumerate(legs)
        },
        external_contract_ids={leg.leg_index: f"conid-{leg.leg_index}" for leg in legs},
    )


def _view() -> ShadowDecisionView:
    return ShadowDecisionView(
        direction="bullish",
        volatility_view="long_vol",
        expected_move_intent="large_move",
        confidence="medium",
        reasoning="synthetic test view",
        evidence_refs={"filings": [1, 2]},
        llm_provider="deepseek",
        llm_model="deepseek-v4-flash",
        prompt_version="p-test-1",
    )


def _generate(db, event, candidates):
    return generate_shadow_decision(
        db,
        earnings_calendar_event_id=event.id,
        ticker="TSTX",
        company_name="Test Co",
        legal_decision_window_at=WINDOW,
        as_of=WINDOW,
        view=_view(),
        candidates=candidates,
        underlying_price=Decimal("100"),
        underlying_quote_at=WINDOW,
        market_data_quality="delayed",
    )


class TestShadowDecisionFreeze:
    def test_freezes_decision_with_the_structured_market_view(self, db_session, calendar_event):
        """Section 4/73 -- the primary gap V4.4B's replay exposed: the
        view must be persisted as structured data, not prose alone."""
        result = _generate(db_session, calendar_event, [
            _candidate("c1", "long_straddle", (_leg(0, "buy", "call", "100"),)),
        ])
        assert result.status == "RANKED"
        row = db_session.get(V4ShadowDecision, result.decision_id)
        assert row.view_direction == "bullish"
        assert row.view_volatility == "long_vol"
        assert row.view_reasoning == "synthetic test view"
        assert row.view_evidence_refs == {"filings": [1, 2]}
        assert row.decision_view_schema_version == DECISION_VIEW_SCHEMA_VERSION

    def test_records_full_model_and_prompt_provenance(self, db_session, calendar_event):
        """Section 5 -- reproducibility must not depend on reading env
        vars after the fact."""
        result = _generate(db_session, calendar_event, [
            _candidate("c1", "long_straddle", (_leg(0, "buy", "call", "100"),)),
        ])
        row = db_session.get(V4ShadowDecision, result.decision_id)
        assert row.llm_provider == "deepseek"
        assert row.llm_model == "deepseek-v4-flash"
        assert row.prompt_version == "p-test-1"

    def test_records_every_methodology_version(self, db_session, calendar_event):
        """Section 19 -- no silent version drift."""
        result = _generate(db_session, calendar_event, [
            _candidate("c1", "long_straddle", (_leg(0, "buy", "call", "100"),)),
        ])
        row = db_session.get(V4ShadowDecision, result.decision_id)
        for field in (
            "engine_version", "shadow_schema_version", "strategy_semantics_version",
            "compatibility_version", "strike_engine_version", "geometry_version",
            "valuation_version", "scenario_grid_version", "ranking_version",
        ):
            assert getattr(row, field), f"{field} not persisted"
        assert row.ranking_version == "v4-4b-t1-executable-ranking-v1"
        assert "v2" in row.scenario_grid_version

    def test_freezes_the_complete_candidate_set_not_only_rank_1(
        self, db_session, calendar_event
    ):
        """Section 6/24 -- discarding the losers would make future
        head-to-head analysis impossible."""
        result = _generate(db_session, calendar_event, [
            _candidate("c1", "long_straddle", (_leg(0, "buy", "call", "100"),)),
            _candidate("c2", "long_call", (_leg(0, "buy", "call", "105", bid="0.50", ask="0.60"),)),
            _candidate("c3", "long_put", (_leg(0, "buy", "put", "95", bid="0.40", ask="0.55"),)),
        ])
        rows = (
            db_session.query(V4ShadowCandidate)
            .filter_by(shadow_decision_id=result.decision_id)
            .all()
        )
        assert len(rows) == 3
        assert result.candidate_count == 3
        ranks = sorted(r.rank for r in rows if r.rank is not None)
        assert ranks == list(range(1, len(ranks) + 1))

    def test_persists_per_leg_point_in_time_evidence(self, db_session, calendar_event):
        """Sections 20/75 -- real per-leg timestamps, conId, required
        side and its price."""
        result = _generate(db_session, calendar_event, [
            _candidate(
                "c1", "call_debit_spread",
                (_leg(0, "buy", "call", "100"), _leg(1, "sell", "call", "105")),
                skew_seconds=3,
            ),
        ])
        candidate = (
            db_session.query(V4ShadowCandidate)
            .filter_by(shadow_decision_id=result.decision_id).one()
        )
        legs = (
            db_session.query(V4ShadowCandidateLeg)
            .filter_by(shadow_candidate_id=candidate.id).order_by(V4ShadowCandidateLeg.leg_index)
            .all()
        )
        assert [leg.required_side for leg in legs] == ["ask", "bid"]
        assert legs[0].required_side_price == Decimal("2.20")   # buy pays ASK
        assert legs[1].required_side_price == Decimal("2.00")   # sell receives BID
        assert all(leg.external_contract_id for leg in legs)
        assert all(leg.retrieved_at is not None for leg in legs)
        assert candidate.max_leg_timestamp_skew_seconds == Decimal(3)
        assert candidate.earliest_leg_observed_at < candidate.latest_leg_observed_at

    def test_persists_core_and_stress_metrics_separately(self, db_session, calendar_event):
        """Sections 16/17 -- stress must never be folded into a core
        statistic."""
        result = _generate(db_session, calendar_event, [
            _candidate("c1", "long_straddle", (_leg(0, "buy", "call", "100"),)),
        ])
        row = (
            db_session.query(V4ShadowCandidate)
            .filter_by(shadow_decision_id=result.decision_id).one()
        )
        assert row.core_scenarios_valued is not None
        assert row.core_region_count == 7          # V4.4A's core grid, untouched
        assert row.stress_scenarios_valued is not None
        assert row.stress_scenarios_valued > 0     # stress evaluated separately

    def test_persists_deterministic_rank_explanation(self, db_session, calendar_event):
        """Sections 76/77 -- no LLM, no hidden state, no random tie-break."""
        result = _generate(db_session, calendar_event, [
            _candidate("c1", "long_straddle", (_leg(0, "buy", "call", "100"),)),
            _candidate("c2", "long_call", (_leg(0, "buy", "call", "105", bid="0.50", ask="0.60"),)),
        ])
        top = (
            db_session.query(V4ShadowCandidate)
            .filter_by(shadow_decision_id=result.decision_id, rank=1).one()
        )
        assert top.rank_explanation
        assert top.ranking_key is not None

    def test_delayed_provenance_is_preserved(self, db_session, calendar_event):
        """Sections 21/55 -- must be stratifiable later if entitlements
        change."""
        result = _generate(db_session, calendar_event, [
            _candidate("c1", "long_straddle", (_leg(0, "buy", "call", "100"),)),
        ])
        decision = db_session.get(V4ShadowDecision, result.decision_id)
        assert decision.market_data_quality == "delayed"
        assert decision.source_provider == "ibkr_tws"
        leg = db_session.query(V4ShadowCandidateLeg).first()
        assert leg.source_provider == "ibkr_tws"


class TestNoActionIsAnOutcome:
    def test_no_rankable_candidate_yields_no_action_not_failure(
        self, db_session, calendar_event
    ):
        """Sections 25/54 -- never force a trade to pad the sample, and
        never count a valid NO_ACTION as a failed run."""
        result = _generate(db_session, calendar_event, [
            # Missing ASK on a buy leg -> not executable now.
            _candidate("c1", "long_call", (_leg(0, "buy", "call", "100", ask=None),)),
        ])
        assert result.status == "NO_ACTION"
        assert result.rank_1_candidate_id is None
        row = db_session.get(V4ShadowDecision, result.decision_id)
        assert row.status == "NO_ACTION"
        assert row.failure_category is None          # NOT a failure
        assert "QUOTE_INCOMPLETE" in row.no_action_reason

    def test_missing_iv_is_reported_as_its_own_reason(self, db_session, calendar_event):
        result = _generate(db_session, calendar_event, [
            _candidate("c1", "long_call", (_leg(0, "buy", "call", "100", iv=None),)),
        ])
        assert result.status == "NO_ACTION"
        row = db_session.get(V4ShadowDecision, result.decision_id)
        assert "MISSING_IV" in row.no_action_reason

    def test_non_rankable_candidates_are_still_frozen(self, db_session, calendar_event):
        """Even a NO_ACTION decision keeps its full evidence."""
        result = _generate(db_session, calendar_event, [
            _candidate("c1", "long_call", (_leg(0, "buy", "call", "100", ask=None),)),
        ])
        rows = (
            db_session.query(V4ShadowCandidate)
            .filter_by(shadow_decision_id=result.decision_id).all()
        )
        assert len(rows) == 1
        assert rows[0].rank is None
        assert rows[0].validity_status == "QUOTE_INCOMPLETE"


class TestObservations:
    def _decision(self, db, event):
        return _generate(db, event, [
            _candidate("c1", "call_debit_spread",
                       (_leg(0, "buy", "call", "100"), _leg(1, "sell", "call", "105"))),
        ])

    def test_entry_observation_uses_ask_for_buy_and_bid_for_sell(
        self, db_session, calendar_event
    ):
        """Section 26 -- the one authoritative entry rule."""
        result = self._decision(db_session, calendar_event)
        legs = (_leg(0, "buy", "call", "100"), _leg(1, "sell", "call", "105"))
        obs = record_observation(
            db_session, shadow_decision_id=result.decision_id, phase="ENTRY",
            candidate_id="c1", legs=legs, observed_at=WINDOW, market_data_quality="delayed",
        )
        assert obs.status == "OBSERVED"
        # buy pays ASK 2.20*100, sell receives BID 2.00*100 -> net 20
        assert obs.net_executable_value == Decimal("20.000000") or obs.net_executable_value == 20
        sides = [leg_row["required_side"] for leg_row in obs.legs_json["legs"]]
        assert sides == ["ask", "bid"]

    def test_exit_observation_inverts_the_sides(self, db_session, calendar_event):
        """Section 27/12 -- closing a long sells into the BID; closing a
        short buys back at the ASK."""
        result = self._decision(db_session, calendar_event)
        legs = (_leg(0, "buy", "call", "100"), _leg(1, "sell", "call", "105"))
        obs = record_observation(
            db_session, shadow_decision_id=result.decision_id, phase="EXIT",
            candidate_id="c1", legs=legs, observed_at=WINDOW + timedelta(days=1),
        )
        sides = [leg_row["required_side"] for leg_row in obs.legs_json["legs"]]
        assert sides == ["bid", "ask"]

    def test_missing_required_side_is_not_executable_never_a_midpoint(
        self, db_session, calendar_event
    ):
        """Sections 26/78/79 -- record the failure honestly rather than
        substituting a midpoint or a last price."""
        result = self._decision(db_session, calendar_event)
        legs = (_leg(0, "buy", "call", "100", ask=None),)
        obs = record_observation(
            db_session, shadow_decision_id=result.decision_id, phase="ENTRY",
            candidate_id="c1", legs=legs, observed_at=WINDOW,
        )
        assert obs.status == "NOT_EXECUTABLE"
        assert obs.net_executable_value is None
        assert "no midpoint" in obs.failure_detail

    def test_observation_is_recorded_once_per_phase(self, db_session, calendar_event):
        """Section 47 -- idempotency at the DB level."""
        from sqlalchemy.exc import IntegrityError

        result = self._decision(db_session, calendar_event)
        legs = (_leg(0, "buy", "call", "100"),)
        record_observation(
            db_session, shadow_decision_id=result.decision_id, phase="ENTRY",
            candidate_id="c1", legs=legs, observed_at=WINDOW,
        )
        with pytest.raises(IntegrityError):
            record_observation(
                db_session, shadow_decision_id=result.decision_id, phase="ENTRY",
                candidate_id="c1", legs=legs, observed_at=WINDOW,
            )


class TestIdempotencyAndIsolation:
    def test_duplicate_shadow_decision_for_same_window_is_idempotent(
        self, db_session, calendar_event
    ):
        """Section 47 -- a scheduler retry must not create a second
        decision for the same event/window/engine. It is reported as
        ALREADY_GENERATED, deliberately NOT as a failure: retrying is
        correct behaviour and must not be counted or alerted as an error
        (the same distinction Section 54 draws for NO_ACTION)."""
        candidates = [_candidate("c1", "long_straddle", (_leg(0, "buy", "call", "100"),))]
        first = _generate(db_session, calendar_event, candidates)
        assert first.status == "RANKED"

        second = _generate(db_session, calendar_event, candidates)
        assert second.status == "ALREADY_GENERATED"
        assert second.failure_category is None
        assert second.decision_id == first.decision_id

        # Exactly one frozen decision, and its candidate set was not
        # duplicated either.
        assert (
            db_session.query(V4ShadowDecision)
            .filter_by(earnings_calendar_event_id=calendar_event.id)
            .count()
            == 1
        )
        assert (
            db_session.query(V4ShadowCandidate)
            .filter_by(shadow_decision_id=first.decision_id)
            .count()
            == 1
        )

    def test_shadow_generation_never_writes_official_v3_evidence(
        self, db_session, calendar_event
    ):
        """The whole point: V3 evidence counts must be untouched."""
        from models.decision_snapshot import DecisionSnapshot

        before = db_session.query(DecisionSnapshot).count()
        _generate(db_session, calendar_event, [
            _candidate("c1", "long_straddle", (_leg(0, "buy", "call", "100"),)),
        ])
        assert db_session.query(DecisionSnapshot).count() == before

    def test_a_shadow_failure_returns_a_result_and_never_raises(
        self, db_session, calendar_event
    ):
        """Section 33 -- V4 failure must not propagate into the official
        path. A malformed candidate is recorded, not raised."""
        broken = _candidate("bad", "long_call", (_leg(0, "buy", "call", "100"),))
        object.__setattr__(broken.context, "legs", "not-a-tuple")  # force an internal error
        result = _generate(db_session, calendar_event, [broken])
        assert result.status == "FAILED"
        assert result.failure_category == "INTERNAL_ERROR"


class TestObservationCounts:
    def test_observations_are_queryable_per_decision(self, db_session, calendar_event):
        result = _generate(db_session, calendar_event, [
            _candidate("c1", "long_straddle", (_leg(0, "buy", "call", "100"),)),
        ])
        record_observation(
            db_session, shadow_decision_id=result.decision_id, phase="ENTRY",
            candidate_id="c1", legs=(_leg(0, "buy", "call", "100"),), observed_at=WINDOW,
        )
        count = (
            db_session.query(V4ShadowObservation)
            .filter_by(shadow_decision_id=result.decision_id).count()
        )
        assert count == 1
