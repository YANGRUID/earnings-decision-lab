"""V4.5 final wiring -- per-event shadow orchestration
(Sections 3, 7, 12, 14, 18, 19).

Drives the real orchestration against synthetic point-in-time data on the
isolated test database. No live IBKR socket is opened (conftest's guard
would refuse one), no official V3 row is touched, and no order exists.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from models.v4_shadow import (
    V4ShadowCandidate,
    V4ShadowDecision,
    V4ShadowObservation,
    V4ShadowRunEvent,
)
from services.v4_shadow import ShadowCandidateInput, ShadowDecisionView
from services.v4_shadow_assembler import AssemblyResult
from services.v4_shadow_orchestration import run_shadow_decisions_for_due_events

NOW = datetime(2026, 9, 10, 19, 55, tzinfo=UTC)
EXPIRATION = date(2026, 9, 18)


@pytest.fixture
def company(db_session):
    from models.company import Company

    row = Company(ticker="TSTX", name="Test Co")
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def event(db_session):
    from models.earnings_calendar_event import EarningsCalendarEvent

    row = EarningsCalendarEvent(
        symbol="TSTX",
        company_name="Test Co",
        earnings_date=date(2026, 9, 10),
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db_session.add(row)
    db_session.flush()
    return row


@pytest.fixture
def thesis(db_session, company):
    """Research readiness -- the gate requires a real prepared thesis."""
    from models.ai_thesis_version import AIThesisVersion

    row = AIThesisVersion(
        company_id=company.id,
        business_context="ctx",
        historical_earnings_pattern="pattern",
        guidance_trend="trend",
        key_risks="risks",
        market_setup="setup",
        disclaimer="d",
        citations=[],
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _em_context() -> ExpectedMoveContext:
    return ExpectedMoveContext(
        spot=Decimal("100"), observed_at=NOW,
        implied_move_available=True, implied_move_dollars=Decimal("5"),
        implied_move_pct=Decimal("0.05"), upper_implied_boundary=Decimal("105"),
        lower_implied_boundary=Decimal("95"), implied_move_source="atm_straddle",
        implied_move_result=None, historical_sample_n=8,
        historical_evidence_quality="adequate",
        historical_median_abs_move_pct=Decimal("0.04"),
        historical_median_upper_boundary=Decimal("104"),
        historical_median_lower_boundary=Decimal("96"),
        historical_quantiles=None, historical_move_stats=None, context_version="test",
    )


def _leg(index, action, right, strike, bid="2.00", ask="2.20", iv="0.40"):
    return V4T1LegInput(
        leg_index=index, action=action, right=right, strike=Decimal(strike),
        quantity=1, multiplier=Decimal("100"),
        entry_bid=Decimal(bid) if bid else None,
        entry_ask=Decimal(ask) if ask else None,
        entry_last=None, entry_iv=Decimal(iv) if iv else None,
        entry_delta=None, entry_gamma=None, entry_theta=None, entry_vega=None,
        market_data_quality="delayed", external_contract_id=f"conid-{index}",
    )


def _candidate(candidate_id, legs):
    context = V4T1ValuationContext(
        ticker="TSTX", underlying_price=Decimal("100"), observed_at=NOW,
        entry_timestamp=NOW, expected_exit_timestamp=NOW + timedelta(days=1),
        strategy="long_straddle", expiration=EXPIRATION, legs=legs,
        expected_move_context=_em_context(),
    )
    return ShadowCandidateInput(
        candidate_id=candidate_id, context=context,
        leg_retrieved_at={leg.leg_index: NOW for leg in legs},
        external_contract_ids={leg.leg_index: f"conid-{leg.leg_index}" for leg in legs},
    )


def _assembly(candidates) -> AssemblyResult:
    result = AssemblyResult(candidates=candidates, expiration=EXPIRATION)
    result.underlying_price = Decimal("100")
    result.underlying_quote_at = NOW
    result.market_data_quality = "delayed"
    return result


def _view() -> ShadowDecisionView:
    return ShadowDecisionView(
        direction="bullish", volatility_view="long_vol", expected_move_intent="large_move",
        confidence="medium", reasoning="synthetic", evidence_refs={"ai_thesis_version_id": 1},
        llm_provider="deepseek", llm_model="deepseek-v4-flash", prompt_version="decision-view-v1",
    )


_USE_DEFAULT_VIEW = object()


def _run(db, event, monkeypatch, *, assembly, view=_USE_DEFAULT_VIEW, due=True):
    from core.config import get_settings

    monkeypatch.setattr(
        "services.v4_shadow_orchestration.assemble_shadow_candidates",
        lambda **kwargs: assembly,
    )
    return run_shadow_decisions_for_due_events(
        db,
        get_settings(),
        now=NOW,
        provider=object(),  # never used -- assembly is injected
        view_generator=lambda *a, **k: (
            _view() if view is _USE_DEFAULT_VIEW else view
        ),
        due_predicate=lambda e, n: due,
        candidate_events=[event],
    )


class TestPerEventOrchestration:
    def test_freezes_decision_candidates_legs_and_entry_observation(
        self, db_session, company, event, thesis, monkeypatch
    ):
        """Section 3 -- the job must drive real orchestration, not merely
        record a scheduler success."""
        assembly = _assembly([
            _candidate("c1", (_leg(0, "buy", "call", "100"), _leg(1, "buy", "put", "100"))),
            _candidate("c2", (_leg(0, "buy", "call", "105"),)),
        ])
        summary = _run(db_session, event, monkeypatch, assembly=assembly)

        assert summary.ranked == 1
        decision = db_session.query(V4ShadowDecision).one()
        assert decision.earnings_calendar_event_id == event.id   # Section 6
        assert decision.status == "RANKED"
        assert decision.rank_1_candidate_id is not None
        # Section 12 -- the COMPLETE candidate set, not only rank #1.
        assert db_session.query(V4ShadowCandidate).count() == 2
        # Section 11 -- rank #1's executable entry observation.
        obs = db_session.query(V4ShadowObservation).filter_by(phase="ENTRY").one()
        assert obs.status == "OBSERVED"
        assert obs.candidate_id == decision.rank_1_candidate_id

    def test_persists_the_decision_view_and_provenance(
        self, db_session, company, event, thesis, monkeypatch
    ):
        _run(db_session, event, monkeypatch, assembly=_assembly([
            _candidate("c1", (_leg(0, "buy", "call", "100"),))
        ]))
        decision = db_session.query(V4ShadowDecision).one()
        assert decision.view_direction == "bullish"
        assert decision.view_volatility == "long_vol"
        assert decision.llm_provider == "deepseek"
        assert decision.prompt_version == "decision-view-v1"

    def test_delayed_provenance_is_preserved(
        self, db_session, company, event, thesis, monkeypatch
    ):
        """Section 10 -- never inferred as LIVE from a live account."""
        _run(db_session, event, monkeypatch, assembly=_assembly([
            _candidate("c1", (_leg(0, "buy", "call", "100"),))
        ]))
        decision = db_session.query(V4ShadowDecision).one()
        assert decision.market_data_quality == "delayed"
        assert decision.source_provider == "ibkr_tws"

    def test_ranking_version_v1_is_recorded_unchanged(
        self, db_session, company, event, thesis, monkeypatch
    ):
        """Section 13 -- the live wiring must not alter ranking v1."""
        _run(db_session, event, monkeypatch, assembly=_assembly([
            _candidate("c1", (_leg(0, "buy", "call", "100"),))
        ]))
        decision = db_session.query(V4ShadowDecision).one()
        assert decision.ranking_version == "v4-4b-t1-executable-ranking-v1"


class TestGates:
    def test_not_due_event_is_skipped_entirely(
        self, db_session, company, event, thesis, monkeypatch
    ):
        """Section 5 -- V4 uses V3's own legal window; an event outside
        it produces nothing at all."""
        summary = _run(
            db_session, event, monkeypatch,
            assembly=_assembly([_candidate("c1", (_leg(0, "buy", "call", "100"),))]),
            due=False,
        )
        assert summary.evaluated == 0
        assert db_session.query(V4ShadowDecision).count() == 0

    def test_research_not_ready_is_recorded_not_forced(
        self, db_session, company, event, monkeypatch
    ):
        """Section 7 -- no thesis fixture here, so research is genuinely
        not ready. Must record the reason, never fabricate a view."""
        summary = _run(db_session, event, monkeypatch, assembly=_assembly([]))
        assert summary.research_not_ready == 1
        assert db_session.query(V4ShadowDecision).count() == 0
        evt = db_session.query(V4ShadowRunEvent).one()
        assert evt.category == "RESEARCH_NOT_READY"
        assert evt.retryable is True

    def test_no_rankable_candidate_yields_no_action_not_failure(
        self, db_session, company, event, thesis, monkeypatch
    ):
        """Section 14 -- NO_ACTION is a valid result, not a failure."""
        summary = _run(db_session, event, monkeypatch, assembly=_assembly([
            # Missing ASK on a buy leg -> not executable now.
            _candidate("c1", (_leg(0, "buy", "call", "100", ask=None),))
        ]))
        assert summary.no_action == 1
        assert summary.failed == 0
        decision = db_session.query(V4ShadowDecision).one()
        assert decision.status == "NO_ACTION"
        assert decision.failure_category is None
        # Evidence is still frozen for a NO_ACTION decision.
        assert db_session.query(V4ShadowCandidate).count() == 1

    def test_assembly_failure_is_recorded_without_freezing_a_decision(
        self, db_session, company, event, thesis, monkeypatch
    ):
        failed = AssemblyResult()
        failed.failure_category = "MARKET_DATA_UNAVAILABLE"
        failed.failure_detail = "no underlying quote"
        summary = _run(db_session, event, monkeypatch, assembly=failed)
        assert summary.failed == 1
        assert db_session.query(V4ShadowDecision).count() == 0
        evt = db_session.query(V4ShadowRunEvent).one()
        assert evt.category == "MARKET_DATA_UNAVAILABLE"

    def test_view_generation_failure_is_recorded_honestly(
        self, db_session, company, event, thesis, monkeypatch
    ):
        summary = _run(
            db_session, event, monkeypatch,
            assembly=_assembly([_candidate("c1", (_leg(0, "buy", "call", "100"),))]),
            view=None,
        )
        # view=None means the generator returned nothing.
        assert summary.failed == 1
        assert db_session.query(V4ShadowDecision).count() == 0


class TestIdempotencyAndRecovery:
    def test_second_run_does_not_duplicate_anything(
        self, db_session, company, event, thesis, monkeypatch
    ):
        """Section 18/19 -- a retry after a completed run must not create
        a second decision, candidate set, or entry observation."""
        assembly = _assembly([_candidate("c1", (_leg(0, "buy", "call", "100"),))])
        first = _run(db_session, event, monkeypatch, assembly=assembly)
        assert first.ranked == 1

        second = _run(db_session, event, monkeypatch, assembly=assembly)
        assert second.already_generated == 1
        assert second.ranked == 0

        assert db_session.query(V4ShadowDecision).count() == 1
        assert db_session.query(V4ShadowCandidate).count() == 1
        assert db_session.query(V4ShadowObservation).filter_by(phase="ENTRY").count() == 1

    def test_one_events_failure_does_not_stop_the_run(
        self, db_session, company, event, thesis, monkeypatch
    ):
        """Section 4 -- a per-event failure is contained; the loop
        continues rather than aborting the whole shadow run."""
        from core.config import get_settings
        from models.earnings_calendar_event import EarningsCalendarEvent

        # Distinct (symbol, earnings_date) -- that pair is uniquely
        # constrained, so reusing the first event's would fail on insert
        # rather than exercising the per-event isolation this test is for.
        second_event = EarningsCalendarEvent(
            symbol="TSTY", company_name="Test Co Two", earnings_date=date(2026, 9, 11),
            earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
        )
        from models.ai_thesis_version import AIThesisVersion
        from models.company import Company

        second_company = Company(ticker="TSTY", name="Test Co Two")
        db_session.add_all([second_event, second_company])
        db_session.flush()
        db_session.add(
            AIThesisVersion(
                company_id=second_company.id, business_context="ctx",
                historical_earnings_pattern="p", guidance_trend="t", key_risks="r",
                market_setup="s", disclaimer="d", citations=[],
                provider="deepseek", model="deepseek-v4-flash",
            )
        )
        db_session.flush()

        calls = {"n": 0}

        def _flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("simulated assembly explosion")
            return _assembly([_candidate("c1", (_leg(0, "buy", "call", "100"),))])

        monkeypatch.setattr(
            "services.v4_shadow_orchestration.assemble_shadow_candidates", _flaky
        )
        summary = run_shadow_decisions_for_due_events(
            db_session, get_settings(), now=NOW, provider=object(),
            view_generator=lambda *a, **k: _view(),
            due_predicate=lambda e, n: True,
            candidate_events=[event, second_event],
        )
        assert summary.failed == 1
        assert summary.ranked == 1          # the second event still succeeded
        assert db_session.query(V4ShadowDecision).count() == 1


