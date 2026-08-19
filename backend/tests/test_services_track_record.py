from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.confidence import ConfidenceComponents
from analytics.decision.strategy_scoring import ViewRankedStrategy, score_candidate_for_view
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.company import Company
from models.earnings_event import EarningsEvent
from models.enums import (
    DecisionDirection,
    DecisionVolatilityView,
    OptionType,
    StrategyRiskPreference,
)
from models.price_reaction import PriceReaction
from rag.context import Citation
from schemas.decision import DecisionView
from services.decision_engine import DecisionResult, ScoredStrategy
from services.decision_history import persist_decision
from services.decision_settlement import settle_decision
from services.track_record import compute_track_record

EXP = date(2026, 9, 18)
UNDERLYING = Decimal("100")


def _seed_company(db_session, ticker: str) -> Company:
    cik = str(abs(hash(ticker)) % 900000000 + 100000000)
    company = Company(ticker=ticker, name="ZZ Track Record Co", cik=cik)
    db_session.add(company)
    db_session.flush()
    return company


def _seed_event(db_session, company: Company, *, earnings_date: date, move_pct: Decimal) -> None:
    event = EarningsEvent(
        company_id=company.id, fiscal_year=2026, fiscal_quarter=3, earnings_date=earnings_date
    )
    db_session.add(event)
    db_session.flush()
    reaction = PriceReaction(
        earnings_event_id=event.id,
        next_day_move_pct=move_pct,
        five_day_move_pct=move_pct,
        source_provider="test",
        retrieved_at=datetime.now(UTC),
    )
    db_session.add(reaction)
    db_session.commit()


def _long_call() -> StrategyCandidate:
    legs = [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("2"))]
    return StrategyCandidate(
        category=StrategyCategory.LONG_CALL,
        legs=tuple(legs),
        analysis=analyze(legs),
        expiration=EXP,
        underlying_price=UNDERLYING,
    )


def _decision_result(direction: str, confidence_total: int) -> DecisionResult:
    candidate = _long_call()
    # Distribute confidence roughly evenly across the 5 real components so
    # the *total* matches confidence_total exactly, for exact bucket tests.
    parts = [confidence_total // 5] * 4
    parts.append(confidence_total - sum(parts))
    breakdown, target_price, payoff, compat = score_candidate_for_view(
        candidate,
        direction=DecisionDirection(direction),
        volatility_view=DecisionVolatilityView.NEUTRAL_VOL,
        implied_move_pct=Decimal("0.05"),
        historical_move_pcts=[],
        has_bid_ask=True,
        market_data_quality="live",
    )
    ranked = ViewRankedStrategy(candidate, 1, target_price, payoff, breakdown, compat)
    scored = ScoredStrategy(ranked=ranked, why=["w"], risks=["r"])
    return DecisionResult(
        view=DecisionView(
            direction=direction,
            volatility_view="long_vol",
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
                ticker="ZZTRACK",
                filing_type="10-K",
                filing_date=date(2026, 2, 1),
                section="Item 1A",
                source_url="https://example.com",
            )
        ],
        confidence=ConfidenceComponents(*parts),
        generated_at=datetime(2026, 8, 1, tzinfo=UTC),
        provider="deepseek",
        model="stub",
        thesis_version_id=None,
        estimate_snapshot_id=None,
        volatility_snapshot_id=None,
        expiration=EXP,
        underlying_price=UNDERLYING,
        implied_move_pct=Decimal("0.05"),
        risk_preference=StrategyRiskPreference.DEFINED_RISK_ONLY,
        recommended=scored,
        alternatives=[],
    )


def _make_settled_decision(
    db_session,
    company: Company,
    *,
    direction: str,
    confidence: int,
    move_pct: Decimal,
    earnings_date: date,
):
    result = _decision_result(direction, confidence)
    decision = persist_decision(db_session, company=company, result=result)
    decision.created_at = datetime(2026, 8, 1, tzinfo=UTC)
    db_session.add(decision)
    db_session.commit()
    _seed_event(db_session, company, earnings_date=earnings_date, move_pct=move_pct)
    return settle_decision(db_session, decision)


class TestComputeTrackRecord:
    def test_no_settled_decisions_returns_zero_sample_none_rates(self, db_session):
        summary = compute_track_record(db_session, ticker="ZZTRACKNONE")
        assert summary.evaluated_count == 0
        assert summary.directional_accuracy.total == 0
        assert summary.directional_accuracy.pct is None
        assert summary.average_confidence is None

    def test_directional_accuracy_counts_correct_and_incorrect(self, db_session):
        company = _seed_company(db_session, "ZZTRACKDIR")
        _make_settled_decision(
            db_session,
            company,
            direction="bullish",
            confidence=80,
            move_pct=Decimal("0.05"),
            earnings_date=date(2026, 8, 15),
        )
        company2 = _seed_company(db_session, "ZZTRACKDIR2")
        _make_settled_decision(
            db_session,
            company2,
            direction="bullish",
            confidence=80,
            move_pct=Decimal("-0.05"),
            earnings_date=date(2026, 8, 15),
        )

        summary = compute_track_record(db_session)

        assert summary.directional_accuracy.total == 2
        assert summary.directional_accuracy.correct == 1
        assert summary.directional_accuracy.pct == Decimal("0.5")

    def test_bullish_and_bearish_accuracy_are_separate(self, db_session):
        bull_co = _seed_company(db_session, "ZZTRACKBULL")
        _make_settled_decision(
            db_session,
            bull_co,
            direction="bullish",
            confidence=80,
            move_pct=Decimal("0.05"),
            earnings_date=date(2026, 8, 15),
        )
        bear_co = _seed_company(db_session, "ZZTRACKBEAR")
        _make_settled_decision(
            db_session,
            bear_co,
            direction="bearish",
            confidence=80,
            move_pct=Decimal("0.05"),  # wrong for a bearish call
            earnings_date=date(2026, 8, 15),
        )

        summary = compute_track_record(db_session)

        assert summary.bullish_accuracy.correct == 1
        assert summary.bullish_accuracy.total == 1
        assert summary.bearish_accuracy.correct == 0
        assert summary.bearish_accuracy.total == 1

    def test_ticker_filter_scopes_to_one_company(self, db_session):
        company_a = _seed_company(db_session, "ZZTRACKFA")
        _make_settled_decision(
            db_session,
            company_a,
            direction="bullish",
            confidence=80,
            move_pct=Decimal("0.05"),
            earnings_date=date(2026, 8, 15),
        )
        company_b = _seed_company(db_session, "ZZTRACKFB")
        _make_settled_decision(
            db_session,
            company_b,
            direction="bearish",
            confidence=80,
            move_pct=Decimal("0.05"),
            earnings_date=date(2026, 8, 15),
        )

        summary = compute_track_record(db_session, ticker="ZZTRACKFA")

        assert summary.evaluated_count == 1

    def test_high_confidence_bucket_excludes_low_confidence(self, db_session):
        company_hi = _seed_company(db_session, "ZZTRACKHI")
        _make_settled_decision(
            db_session,
            company_hi,
            direction="bullish",
            confidence=90,
            move_pct=Decimal("0.05"),
            earnings_date=date(2026, 8, 15),
        )
        company_lo = _seed_company(db_session, "ZZTRACKLO")
        _make_settled_decision(
            db_session,
            company_lo,
            direction="bullish",
            confidence=30,
            move_pct=Decimal("0.05"),
            earnings_date=date(2026, 8, 15),
        )

        summary = compute_track_record(db_session)

        assert summary.high_confidence_accuracy.total == 1  # only the confidence=90 one

    def test_average_confidence_is_real_mean(self, db_session):
        company_a = _seed_company(db_session, "ZZTRACKAVGA")
        _make_settled_decision(
            db_session,
            company_a,
            direction="bullish",
            confidence=80,
            move_pct=Decimal("0.05"),
            earnings_date=date(2026, 8, 15),
        )
        company_b = _seed_company(db_session, "ZZTRACKAVGB")
        _make_settled_decision(
            db_session,
            company_b,
            direction="bullish",
            confidence=60,
            move_pct=Decimal("0.05"),
            earnings_date=date(2026, 8, 15),
        )

        summary = compute_track_record(db_session)

        assert summary.average_confidence == Decimal("70")

    def test_strategy_win_rate_always_unavailable(self, db_session):
        company = _seed_company(db_session, "ZZTRACKWIN")
        _make_settled_decision(
            db_session,
            company,
            direction="bullish",
            confidence=80,
            move_pct=Decimal("0.05"),
            earnings_date=date(2026, 8, 15),
        )

        summary = compute_track_record(db_session)

        assert summary.strategy_win_rate_available is False

    def test_confidence_calibration_buckets_real_decisions(self, db_session):
        company = _seed_company(db_session, "ZZTRACKCAL")
        _make_settled_decision(
            db_session,
            company,
            direction="bullish",
            confidence=85,
            move_pct=Decimal("0.05"),
            earnings_date=date(2026, 8, 15),
        )

        summary = compute_track_record(db_session)

        bucket_80_89 = next(b for b in summary.confidence_calibration if b.label == "80-89")
        assert bucket_80_89.rate.total == 1
        assert bucket_80_89.rate.correct == 1

    def test_last_10_window_limits_result_set(self, db_session):
        # One company per decision -- uq_earnings_event is
        # (company_id, fiscal_year, fiscal_quarter), so 12 real events for
        # a single company would need 12 distinct fiscal quarters; a fresh
        # company per decision is simpler and just as real.
        for i in range(12):
            company = _seed_company(db_session, f"ZZTRACKWIN10_{i}")
            _make_settled_decision(
                db_session,
                company,
                direction="bullish",
                confidence=80,
                move_pct=Decimal("0.05"),
                earnings_date=date(2026, 8, 1 + i),
            )

        summary = compute_track_record(db_session, window="last_10")

        assert summary.evaluated_count == 10
