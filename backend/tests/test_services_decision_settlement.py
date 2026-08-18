from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.confidence import ConfidenceComponents
from analytics.decision.strategy_scoring import ViewRankedStrategy, score_candidate_for_view
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.company import Company
from models.earnings_event import EarningsEvent
from models.enums import DecisionDirection, DecisionStatus, OptionType, StrategyRiskPreference
from models.price_reaction import PriceReaction
from rag.context import Citation
from schemas.decision import DecisionView
from services.decision_engine import DecisionResult, ScoredStrategy
from services.decision_history import persist_decision
from services.decision_settlement import find_settlement_event, settle_all_pending, settle_decision

EXP = date(2026, 9, 18)
UNDERLYING = Decimal("100")


def _seed_company(db_session, ticker: str = "ZZDSETL") -> Company:
    cik = str(abs(hash(ticker)) % 900000000 + 100000000)
    company = Company(ticker=ticker, name="ZZ Decision Settlement Co", cik=cik)
    db_session.add(company)
    db_session.flush()
    return company


def _seed_earnings_event(
    db_session, company: Company, *, earnings_date: date, next_day_move_pct: Decimal | None
) -> EarningsEvent:
    event = EarningsEvent(
        company_id=company.id, fiscal_year=2026, fiscal_quarter=3, earnings_date=earnings_date
    )
    db_session.add(event)
    db_session.flush()
    reaction = PriceReaction(
        earnings_event_id=event.id,
        close_price_before=Decimal("95"),
        next_day_close=Decimal("95") * (1 + next_day_move_pct) if next_day_move_pct else None,
        next_day_move_pct=next_day_move_pct,
        five_day_move_pct=next_day_move_pct,
        source_provider="test",
        retrieved_at=datetime.now(UTC),
    )
    db_session.add(reaction)
    db_session.flush()
    return event


def _long_call_candidate(underlying: Decimal = UNDERLYING) -> StrategyCandidate:
    legs = [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("2"))]
    return StrategyCandidate(
        category=StrategyCategory.LONG_CALL,
        legs=tuple(legs),
        analysis=analyze(legs),
        expiration=EXP,
        underlying_price=underlying,
    )


def _decision_result(
    direction: str = "bullish", underlying: Decimal = UNDERLYING
) -> DecisionResult:
    candidate = _long_call_candidate(underlying)
    breakdown, target_price, payoff, compat = score_candidate_for_view(
        candidate,
        direction=DecisionDirection(direction),
        implied_move_pct=Decimal("0.05"),
        historical_move_pcts=[],
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
    scored = ScoredStrategy(ranked=ranked, why=["why"], risks=["risk"])
    return DecisionResult(
        view=DecisionView(
            direction=direction,
            volatility_view="neutral_vol",
            rationale="r",
            bull_case="b",
            bear_case="b",
            key_catalysts="c",
            key_risks="k",
            disclaimer="d",
        ),
        citations=[
            Citation(
                marker="[1]",
                ticker="ZZDSETL",
                filing_type="10-K",
                filing_date=date(2026, 2, 1),
                section="Item 1A",
                source_url="https://example.com",
            )
        ],
        confidence=ConfidenceComponents(20, 20, 20, 15, 20),
        generated_at=datetime(2026, 8, 1, tzinfo=UTC),
        provider="deepseek",
        model="stub-model",
        thesis_version_id=None,
        estimate_snapshot_id=None,
        volatility_snapshot_id=None,
        expiration=EXP,
        underlying_price=underlying,
        implied_move_pct=Decimal("0.05"),
        risk_preference=StrategyRiskPreference.DEFINED_RISK_ONLY,
        recommended=scored,
        alternatives=[],
    )


class TestFindSettlementEvent:
    def test_finds_event_on_or_after_decision_with_real_reaction(self, db_session):
        company = _seed_company(db_session)
        decision = persist_decision(db_session, company=company, result=_decision_result())
        # Move created_at into the past so a future earnings_date qualifies.
        decision.created_at = datetime(2026, 8, 1, tzinfo=UTC)
        db_session.add(decision)
        db_session.commit()

        event = _seed_earnings_event(
            db_session, company, earnings_date=date(2026, 8, 15), next_day_move_pct=Decimal("0.05")
        )

        found = find_settlement_event(db_session, decision)
        assert found is not None
        assert found.id == event.id

    def test_returns_none_when_no_event_reported_yet(self, db_session):
        company = _seed_company(db_session, ticker="ZZDSETLNONE")
        decision = persist_decision(db_session, company=company, result=_decision_result())

        assert find_settlement_event(db_session, decision) is None

    def test_ignores_events_before_the_decision_was_made(self, db_session):
        company = _seed_company(db_session, ticker="ZZDSETLOLD")
        decision = persist_decision(db_session, company=company, result=_decision_result())
        decision.created_at = datetime(2026, 8, 15, tzinfo=UTC)
        db_session.add(decision)
        db_session.commit()

        _seed_earnings_event(
            db_session, company, earnings_date=date(2026, 8, 1), next_day_move_pct=Decimal("0.05")
        )

        assert find_settlement_event(db_session, decision) is None


class TestSettleDecision:
    def test_bullish_decision_correct_when_move_is_positive(self, db_session):
        company = _seed_company(db_session, ticker="ZZDSETLBULL")
        decision = persist_decision(db_session, company=company, result=_decision_result("bullish"))
        decision.created_at = datetime(2026, 8, 1, tzinfo=UTC)
        db_session.add(decision)
        db_session.commit()
        _seed_earnings_event(
            db_session, company, earnings_date=date(2026, 8, 15), next_day_move_pct=Decimal("0.05")
        )

        result = settle_decision(db_session, decision)

        assert result is not None
        assert result.status == DecisionStatus.SETTLED
        assert result.direction_correct is True
        assert result.actual_next_day_move_pct == Decimal("0.05")
        assert result.settled_at is not None
        # Never fabricated -- this project has no real option entry/exit data.
        assert result.strategy_pnl is None
        assert result.strategy_pnl_available is False

    def test_bullish_decision_incorrect_when_move_is_negative(self, db_session):
        company = _seed_company(db_session, ticker="ZZDSETLBULLWRONG")
        decision = persist_decision(db_session, company=company, result=_decision_result("bullish"))
        decision.created_at = datetime(2026, 8, 1, tzinfo=UTC)
        db_session.add(decision)
        db_session.commit()
        _seed_earnings_event(
            db_session, company, earnings_date=date(2026, 8, 15), next_day_move_pct=Decimal("-0.05")
        )

        result = settle_decision(db_session, decision)

        assert result.direction_correct is False

    def test_neutral_decision_has_no_directional_correctness(self, db_session):
        company = _seed_company(db_session, ticker="ZZDSETLNEUT")
        decision = persist_decision(db_session, company=company, result=_decision_result("neutral"))
        decision.created_at = datetime(2026, 8, 1, tzinfo=UTC)
        db_session.add(decision)
        db_session.commit()
        _seed_earnings_event(
            db_session, company, earnings_date=date(2026, 8, 15), next_day_move_pct=Decimal("0.02")
        )

        result = settle_decision(db_session, decision)

        assert result.direction_correct is None  # no direction was called -- not gradeable

    def test_returns_none_when_no_real_data_yet(self, db_session):
        company = _seed_company(db_session, ticker="ZZDSETLNODATA")
        decision = persist_decision(db_session, company=company, result=_decision_result())

        assert settle_decision(db_session, decision) is None
        assert decision.status == DecisionStatus.OPEN

    def test_idempotent_on_already_settled_decision(self, db_session):
        company = _seed_company(db_session, ticker="ZZDSETLIDEM")
        decision = persist_decision(db_session, company=company, result=_decision_result())
        decision.created_at = datetime(2026, 8, 1, tzinfo=UTC)
        db_session.add(decision)
        db_session.commit()
        _seed_earnings_event(
            db_session, company, earnings_date=date(2026, 8, 15), next_day_move_pct=Decimal("0.05")
        )

        first = settle_decision(db_session, decision)
        second = settle_decision(db_session, decision)

        assert first.settled_at == second.settled_at

    def test_actual_move_exceeded_implied(self, db_session):
        company = _seed_company(db_session, ticker="ZZDSETLEXCEED")
        decision = persist_decision(db_session, company=company, result=_decision_result())
        decision.created_at = datetime(2026, 8, 1, tzinfo=UTC)
        db_session.add(decision)
        db_session.commit()
        # implied_move_pct on this decision is 0.05 -- a 0.10 move exceeds it.
        _seed_earnings_event(
            db_session, company, earnings_date=date(2026, 8, 15), next_day_move_pct=Decimal("0.10")
        )

        result = settle_decision(db_session, decision)

        assert result.actual_move_exceeded_implied is True

    def test_breakeven_met_for_long_call_on_favorable_move(self, db_session):
        company = _seed_company(db_session, ticker="ZZDSETLBE")
        # Long call at strike 100, premium 2 -> breakeven at 102. A move to
        # underlying*(1+0.05) = 105 clears it.
        decision = persist_decision(db_session, company=company, result=_decision_result("bullish"))
        decision.created_at = datetime(2026, 8, 1, tzinfo=UTC)
        db_session.add(decision)
        db_session.commit()
        _seed_earnings_event(
            db_session, company, earnings_date=date(2026, 8, 15), next_day_move_pct=Decimal("0.05")
        )

        result = settle_decision(db_session, decision)

        assert result.breakeven_met is True


class TestSettleAllPending:
    def test_settles_only_ready_decisions(self, db_session):
        company = _seed_company(db_session, ticker="ZZDSETLALL")
        other_company = _seed_company(db_session, ticker="ZZDSETLALLB")
        ready = persist_decision(db_session, company=company, result=_decision_result())
        ready.created_at = datetime(2026, 8, 1, tzinfo=UTC)
        # No earnings event exists yet for other_company -- this decision
        # has nothing to settle against.
        not_ready = persist_decision(db_session, company=other_company, result=_decision_result())
        not_ready.created_at = datetime(2026, 8, 1, tzinfo=UTC)
        db_session.add_all([ready, not_ready])
        db_session.commit()
        _seed_earnings_event(
            db_session, company, earnings_date=date(2026, 8, 15), next_day_move_pct=Decimal("0.03")
        )

        settled = settle_all_pending(db_session)

        settled_ids = {d.id for d in settled}
        assert ready.id in settled_ids
        assert not_ready.id not in settled_ids
