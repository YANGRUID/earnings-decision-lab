from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.confidence import ConfidenceComponents
from analytics.decision.strategy_scoring import ViewRankedStrategy, score_candidate_for_view
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.company import Company
from models.enums import DecisionDirection, DecisionSource, OptionType, StrategyRiskPreference
from rag.context import Citation
from schemas.decision import DecisionView
from services.decision_engine import DecisionResult, ScoredStrategy
from services.decision_history import (
    DecisionListFilters,
    delete_decision,
    get_decision,
    list_all_decisions,
    list_decisions,
    mark_final,
    persist_decision,
)

EXP = date(2026, 9, 18)
UNDERLYING = Decimal("100")


def _seed_company(db_session, ticker: str = "ZZDHIST") -> Company:
    cik = str(abs(hash(ticker)) % 900000000 + 100000000)
    company = Company(ticker=ticker, name="ZZ Decision History Co", cik=cik)
    db_session.add(company)
    db_session.flush()
    return company


def _long_call_candidate() -> StrategyCandidate:
    legs = [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("2"))]
    return StrategyCandidate(
        category=StrategyCategory.LONG_CALL,
        legs=tuple(legs),
        analysis=analyze(legs),
        expiration=EXP,
        underlying_price=UNDERLYING,
    )


def _scored_strategy() -> ScoredStrategy:
    candidate = _long_call_candidate()
    breakdown, target_price, payoff, compat = score_candidate_for_view(
        candidate,
        direction=DecisionDirection.BULLISH,
        implied_move_pct=Decimal("0.10"),
        historical_move_pcts=[Decimal("0.08")],
        has_bid_ask=True,
        market_data_quality="live",
    )
    ranked = ViewRankedStrategy(
        candidate=candidate,
        rank=1,
        target_price=target_price,
        payoff_at_target=payoff,
        score=breakdown,
        move_compatibility=compat,
    )
    return ScoredStrategy(ranked=ranked, why=["real reason"], risks=["real risk"])


def _decision_view() -> DecisionView:
    return DecisionView(
        direction="bullish",
        volatility_view="long_vol",
        rationale="Real rationale grounded in evidence.",
        bull_case="Real bull case.",
        bear_case="Real bear case.",
        key_catalysts="Real catalysts.",
        key_risks="Real risks.",
        disclaimer="This is not investment advice.",
    )


def _decision_result(**overrides) -> DecisionResult:
    defaults = dict(
        view=_decision_view(),
        citations=[
            Citation(
                marker="[1]",
                ticker="ZZDHIST",
                filing_type="10-K",
                filing_date=date(2026, 2, 1),
                section="Item 1A",
                source_url="https://example.com/filing",
            )
        ],
        confidence=ConfidenceComponents(
            evidence_coverage=25,
            consensus_agreement=20,
            historical_consistency=20,
            data_freshness=15,
            options_completeness=20,
        ),
        generated_at=datetime.now(UTC),
        provider="deepseek",
        model="stub-model",
        thesis_version_id=None,
        estimate_snapshot_id=None,
        volatility_snapshot_id=None,
        expiration=EXP,
        underlying_price=UNDERLYING,
        implied_move_pct=Decimal("0.10"),
        risk_preference=StrategyRiskPreference.DEFINED_RISK_ONLY,
        recommended=_scored_strategy(),
        alternatives=[],
    )
    defaults.update(overrides)
    return DecisionResult(**defaults)


class TestPersistDecision:
    def test_persists_all_real_fields(self, db_session):
        company = _seed_company(db_session)
        result = _decision_result()

        row = persist_decision(db_session, company=company, result=result)

        assert row.id is not None
        assert row.company_id == company.id
        assert row.direction == "bullish"
        assert row.volatility_view == "long_vol"
        assert row.confidence_score == 100
        assert row.confidence_components["evidence_coverage"] == 25
        assert row.rationale == "Real rationale grounded in evidence."
        assert row.citations[0]["marker"] == "[1]"
        assert row.decision_source == DecisionSource.AI
        assert row.recommended_strategy_category == "long_call"
        assert row.recommended_strategy_legs[0]["option_type"] == "call"
        assert row.recommended_strategy_score is not None
        assert row.recommended_strategy_why == ["real reason"]
        assert row.status == "open"
        assert row.is_final is False

    def test_no_recommended_strategy_persists_nulls(self, db_session):
        company = _seed_company(db_session, ticker="ZZDHISTNONE")
        result = _decision_result(recommended=None, alternatives=[])

        row = persist_decision(db_session, company=company, result=result)

        assert row.recommended_strategy_category is None
        assert row.recommended_strategy_legs is None

    def test_manual_override_source_is_persisted(self, db_session):
        company = _seed_company(db_session, ticker="ZZDHISTOV")
        result = _decision_result()

        row = persist_decision(
            db_session,
            company=company,
            result=result,
            decision_source=DecisionSource.MANUAL_OVERRIDE,
        )

        assert row.decision_source == DecisionSource.MANUAL_OVERRIDE


class TestListDecisions:
    def test_returns_newest_first_and_never_overwrites(self, db_session):
        company = _seed_company(db_session, ticker="ZZDHISTLIST")
        first = persist_decision(db_session, company=company, result=_decision_result())
        second = persist_decision(db_session, company=company, result=_decision_result())

        rows = list_decisions(db_session, company.id)

        assert [r.id for r in rows] == [second.id, first.id]
        assert first.id != second.id  # two distinct rows -- never an overwrite

    def test_scoped_to_company(self, db_session):
        company_a = _seed_company(db_session, ticker="ZZDHISTA")
        company_b = _seed_company(db_session, ticker="ZZDHISTB")
        persist_decision(db_session, company=company_a, result=_decision_result())
        persist_decision(db_session, company=company_b, result=_decision_result())

        rows = list_decisions(db_session, company_a.id)

        assert len(rows) == 1
        assert rows[0].company_id == company_a.id


class TestGetAndDeleteDecision:
    def test_get_returns_none_for_unknown_id(self, db_session):
        assert get_decision(db_session, 999999999) is None

    def test_delete_removes_the_row(self, db_session):
        company = _seed_company(db_session, ticker="ZZDHISTDEL")
        row = persist_decision(db_session, company=company, result=_decision_result())

        assert delete_decision(db_session, row.id) is True
        assert get_decision(db_session, row.id) is None

    def test_delete_returns_false_for_unknown_id(self, db_session):
        assert delete_decision(db_session, 999999999) is False


class TestMarkFinal:
    def test_marks_one_decision_final_and_unmarks_prior(self, db_session):
        company = _seed_company(db_session, ticker="ZZDHISTFINAL")
        first = persist_decision(db_session, company=company, result=_decision_result())
        second = persist_decision(db_session, company=company, result=_decision_result())

        mark_final(db_session, first.id)
        mark_final(db_session, second.id)

        db_session.refresh(first)
        db_session.refresh(second)
        assert first.is_final is False
        assert second.is_final is True

    def test_does_not_affect_other_companies(self, db_session):
        company_a = _seed_company(db_session, ticker="ZZDHISTFA")
        company_b = _seed_company(db_session, ticker="ZZDHISTFB")
        a_decision = persist_decision(db_session, company=company_a, result=_decision_result())
        b_decision = persist_decision(db_session, company=company_b, result=_decision_result())

        mark_final(db_session, a_decision.id)
        mark_final(db_session, b_decision.id)

        db_session.refresh(a_decision)
        db_session.refresh(b_decision)
        assert a_decision.is_final is True
        assert b_decision.is_final is True

    def test_returns_none_for_unknown_id(self, db_session):
        assert mark_final(db_session, 999999999) is None


class TestListAllDecisions:
    def test_filters_by_ticker(self, db_session):
        company_a = _seed_company(db_session, ticker="ZZDHISTGA")
        company_b = _seed_company(db_session, ticker="ZZDHISTGB")
        persist_decision(db_session, company=company_a, result=_decision_result())
        persist_decision(db_session, company=company_b, result=_decision_result())

        rows = list_all_decisions(db_session, filters=DecisionListFilters(ticker="ZZDHISTGA"))

        assert len(rows) == 1
        assert rows[0].company_id == company_a.id

    def test_is_final_only_filter(self, db_session):
        company = _seed_company(db_session, ticker="ZZDHISTGF")
        first = persist_decision(db_session, company=company, result=_decision_result())
        persist_decision(db_session, company=company, result=_decision_result())
        mark_final(db_session, first.id)

        rows = list_all_decisions(db_session, filters=DecisionListFilters(is_final_only=True))

        assert all(r.is_final for r in rows)
        assert any(r.id == first.id for r in rows)
