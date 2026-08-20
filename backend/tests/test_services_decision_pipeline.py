"""Phase 4.3 -- integration tests for services/decision_pipeline.py: the
real wiring from earnings_calendar_event through eligibility and the
timing gate to a frozen decision_snapshot. generate_decision() itself is
monkeypatched (this codebase has no existing pattern for calling it with
a real LLM in tests -- confirmed no test anywhere does), so what's under
test here is the pipeline's own integration logic: does it check the
right things in the right order, does it call generation only when it
should, and does a real decision_snapshot row land correctly linked when
it does.
"""

from datetime import date, datetime, timedelta
from decimal import Decimal

from analytics.decision.confidence import ConfidenceComponents
from analytics.decision.strategy_scoring import ViewRankedStrategy, score_candidate_for_view
from analytics.earnings_timing import compute_entry_exit_schedule
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.benchmark_portfolio import BenchmarkPortfolio
from models.company import Company
from models.decision_snapshot import DecisionSnapshot
from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import (
    AnnouncementTime,
    DecisionDirection,
    DecisionVolatilityView,
    EarningsCalendarEventStatus,
    EarningsTiming,
    OptionType,
    RiskProfile,
    StrategyRiskPreference,
)
from providers.base import OptionsDataProvider
from providers.types import OptionQuote
from rag.context import Citation
from schemas.decision import DecisionView
from services import decision_pipeline
from services.decision_engine import DecisionResult, ScoredStrategy

EXP = date(2026, 9, 18)
UNDERLYING = Decimal("100")
EARNINGS_DATE = date(2026, 9, 16)


class _FakeOptionsProvider(OptionsDataProvider):
    def get_option_chain(
        self, ticker, as_of, expiration=None, reference_date=None, earnings_anchored=True
    ) -> list[OptionQuote]:
        return []

    def list_available_expirations(self, ticker, after, max_candidates=5) -> list[date]:
        return [EXP]


def _seed_company(db_session, ticker: str = "ZZPIPE") -> Company:
    cik = str(abs(hash(ticker)) % 900000000 + 100000000)
    company = Company(ticker=ticker, name="ZZ Pipeline Test Co", cik=cik)
    db_session.add(company)
    db_session.flush()
    return company


def _seed_event_and_portfolio(
    db_session, symbol: str = "ZZPIPE", **event_overrides
) -> tuple[EarningsCalendarEvent, BenchmarkPortfolio]:
    defaults = dict(
        symbol=symbol,
        company_name="ZZ Pipeline Test Co",
        earnings_date=EARNINGS_DATE,
        earnings_time=EarningsTiming.AMC,
        market_cap=Decimal("3200000000000"),
        country="US",
    )
    defaults.update(event_overrides)
    event = EarningsCalendarEvent(**defaults)
    portfolio = BenchmarkPortfolio(
        name=f"Pipeline Test Portfolio {symbol}",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
    )
    db_session.add_all([event, portfolio])
    db_session.flush()
    return event, portfolio


def _due_now() -> datetime:
    schedule = compute_entry_exit_schedule(EARNINGS_DATE, AnnouncementTime.AFTER_MARKET)
    return schedule.entry_timestamp


def _fixture_decision_result() -> DecisionResult:
    legs = [OptionLeg(OptionType.CALL, Action.BUY, Decimal("100"), Decimal("2"))]
    candidate = StrategyCandidate(
        category=StrategyCategory.LONG_CALL,
        legs=tuple(legs),
        analysis=analyze(legs),
        expiration=EXP,
        underlying_price=UNDERLYING,
    )
    breakdown, target_price, payoff, compat = score_candidate_for_view(
        candidate,
        direction=DecisionDirection.BULLISH,
        volatility_view=DecisionVolatilityView.NEUTRAL_VOL,
        implied_move_pct=Decimal("0.10"),
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
    recommended = ScoredStrategy(ranked=ranked, why=["real reason"], risks=["real risk"])
    return DecisionResult(
        view=DecisionView(
            direction="bullish",
            volatility_view="long_vol",
            rationale="Real rationale.",
            bull_case="Real bull case.",
            bear_case="Real bear case.",
            key_catalysts="Real catalysts.",
            key_risks="Real risks.",
            disclaimer="This is not investment advice.",
        ),
        citations=[
            Citation(
                marker="[1]",
                ticker="ZZPIPE",
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
        generated_at=_due_now(),
        provider="deepseek",
        model="stub-model",
        thesis_version_id=None,
        estimate_snapshot_id=None,
        volatility_snapshot_id=None,
        expiration=EXP,
        underlying_price=UNDERLYING,
        implied_move_pct=Decimal("0.10"),
        risk_preference=StrategyRiskPreference.DEFINED_RISK_ONLY,
        risk_profile=RiskProfile.MODERATE,
        recommended=recommended,
        alternatives=[],
    )


def _stub_generate_decision(monkeypatch, calls: list):
    def _fake(db, llm, embedder, company, **kwargs):
        calls.append(company.ticker)
        return _fixture_decision_result()

    monkeypatch.setattr(decision_pipeline, "generate_decision", _fake)


def test_pipeline_creates_snapshot_for_eligible_and_due_event(db_session, monkeypatch):
    company = _seed_company(db_session)
    event, portfolio = _seed_event_and_portfolio(db_session)
    calls: list = []
    _stub_generate_decision(monkeypatch, calls)

    outcome = decision_pipeline.run_decision_pipeline_for_event(
        db_session, event, portfolio, _FakeOptionsProvider(), llm=object(), embedder=object(),
        now=_due_now(),
    )
    db_session.flush()

    assert outcome.outcome == "created"
    assert outcome.decision_snapshot_id is not None
    assert calls == [company.ticker]

    snapshot = db_session.get(DecisionSnapshot, outcome.decision_snapshot_id)
    assert snapshot.earnings_calendar_event_id == event.id
    assert snapshot.benchmark_portfolio_id == portfolio.id
    assert snapshot.ticker == company.ticker


def test_pipeline_skips_ineligible_event_without_generating(db_session, monkeypatch):
    _seed_company(db_session)
    event, portfolio = _seed_event_and_portfolio(
        db_session, market_cap=Decimal("5000000000")  # $5B, below the $10B floor
    )
    calls: list = []
    _stub_generate_decision(monkeypatch, calls)

    outcome = decision_pipeline.run_decision_pipeline_for_event(
        db_session, event, portfolio, _FakeOptionsProvider(), llm=object(), embedder=object(),
        now=_due_now(),
    )

    assert outcome.outcome == "skipped_ineligible"
    assert outcome.decision_snapshot_id is None
    assert calls == []  # generate_decision must never be called for an ineligible event
    assert event.status == EarningsCalendarEventStatus.UPCOMING  # never modified


def test_pipeline_skips_not_yet_due_event(db_session, monkeypatch):
    _seed_company(db_session)
    event, portfolio = _seed_event_and_portfolio(db_session)
    calls: list = []
    _stub_generate_decision(monkeypatch, calls)

    too_early = _due_now().replace(hour=9, minute=0)
    outcome = decision_pipeline.run_decision_pipeline_for_event(
        db_session, event, portfolio, _FakeOptionsProvider(), llm=object(), embedder=object(),
        now=too_early,
    )

    assert outcome.outcome == "skipped_not_due"
    assert calls == []


def test_pipeline_skips_too_late_event(db_session, monkeypatch):
    """Real look-ahead-bias protection: a scheduler that fires well past
    the safe window must never silently generate a decision using data
    that may have already priced in the reaction."""
    _seed_company(db_session)
    event, portfolio = _seed_event_and_portfolio(db_session)
    calls: list = []
    _stub_generate_decision(monkeypatch, calls)

    too_late = _due_now() + timedelta(hours=3)
    outcome = decision_pipeline.run_decision_pipeline_for_event(
        db_session, event, portfolio, _FakeOptionsProvider(), llm=object(), embedder=object(),
        now=too_late,
    )

    assert outcome.outcome == "skipped_too_late"
    assert calls == []


def test_pipeline_skips_when_no_company_researched(db_session, monkeypatch):
    # deliberately no _seed_company call
    event, portfolio = _seed_event_and_portfolio(db_session)
    calls: list = []
    _stub_generate_decision(monkeypatch, calls)

    outcome = decision_pipeline.run_decision_pipeline_for_event(
        db_session, event, portfolio, _FakeOptionsProvider(), llm=object(), embedder=object(),
        now=_due_now(),
    )

    assert outcome.outcome == "skipped_no_company"
    assert calls == []


def test_pipeline_is_idempotent_for_an_already_frozen_event(db_session, monkeypatch):
    company = _seed_company(db_session)
    event, portfolio = _seed_event_and_portfolio(db_session)
    calls: list = []
    _stub_generate_decision(monkeypatch, calls)

    first = decision_pipeline.run_decision_pipeline_for_event(
        db_session, event, portfolio, _FakeOptionsProvider(), llm=object(), embedder=object(),
        now=_due_now(),
    )
    db_session.flush()
    assert first.outcome == "created"
    assert calls == [company.ticker]

    second = decision_pipeline.run_decision_pipeline_for_event(
        db_session, event, portfolio, _FakeOptionsProvider(), llm=object(), embedder=object(),
        now=_due_now(),
    )

    assert second.outcome == "already_frozen"
    assert second.decision_snapshot_id == first.decision_snapshot_id
    assert calls == [company.ticker]  # still just one call -- no second generation

    count = (
        db_session.query(DecisionSnapshot)
        .filter_by(earnings_calendar_event_id=event.id, benchmark_portfolio_id=portfolio.id)
        .count()
    )
    assert count == 1
