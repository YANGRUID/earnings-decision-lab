"""Phase 4.3 -- unit tests for services/decision_snapshot_freezing.py.
Mirrors tests/test_services_decision_history.py's fixture-DecisionResult
pattern exactly (same construction helpers, adapted) -- no live LLM,
no live provider call, matching this project's testing convention.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.confidence import ConfidenceComponents
from analytics.decision.strategy_scoring import ViewRankedStrategy, score_candidate_for_view
from analytics.options.payoff import Action, OptionLeg, analyze
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.benchmark_portfolio import BenchmarkPortfolio
from models.company import Company
from models.earnings_calendar_event import EarningsCalendarEvent
from models.earnings_event import EarningsEvent
from models.enums import (
    DecisionDirection,
    DecisionVolatilityView,
    EarningsTiming,
    OptionType,
    RiskProfile,
    StrategyRiskPreference,
)
from models.price_reaction import PriceReaction
from models.volatility_snapshot import VolatilitySnapshot
from providers.types import OptionQuote
from rag.context import Citation
from schemas.decision import DecisionView
from services.decision_engine import DecisionResult, ScoredStrategy
from services.decision_snapshot_freezing import (
    ENGINE_VERSION,
    EXPIRATION_SOURCE_V3_RESOLVER,
    PROMPT_VERSION,
    _classify_volatility_regime,
    freeze_decision_snapshot,
)

EXP = date(2026, 9, 18)
UNDERLYING = Decimal("100")


def _seed_company(db_session, ticker: str = "ZZFRZ") -> Company:
    cik = str(abs(hash(ticker)) % 900000000 + 100000000)
    company = Company(ticker=ticker, name="ZZ Freeze Test Co", cik=cik)
    db_session.add(company)
    db_session.flush()
    return company


def _seed_event_and_portfolio(
    db_session, symbol: str = "ZZFRZ"
) -> tuple[EarningsCalendarEvent, BenchmarkPortfolio]:
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name="ZZ Freeze Test Co",
        earnings_date=date(2026, 9, 17),
        earnings_time=EarningsTiming.AMC,
    )
    portfolio = BenchmarkPortfolio(
        name=f"Freeze Test Portfolio {symbol}",
        initial_capital=Decimal("2000.00"),
        cash_balance=Decimal("2000.00"),
    )
    db_session.add_all([event, portfolio])
    db_session.flush()
    return event, portfolio


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
        volatility_view=DecisionVolatilityView.NEUTRAL_VOL,
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
    return ScoredStrategy(
        ranked=ranked,
        why=["real reason"],
        risks=["real risk"],
        why_expiration=["real expiration reason"],
        why_strikes=["real strike reason"],
        why_not_alternative=["real why-not reason"],
    )


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
                ticker="ZZFRZ",
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
        generated_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
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
        recommended=_scored_strategy(),
        alternatives=[],
    )
    defaults.update(overrides)
    return DecisionResult(**defaults)


def test_freeze_decision_snapshot_maps_ai_decision_correctly(db_session):
    company = _seed_company(db_session)
    event, portfolio = _seed_event_and_portfolio(db_session)
    result = _decision_result()

    snapshot = freeze_decision_snapshot(
        db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
    )
    db_session.flush()

    assert snapshot.id is not None
    assert snapshot.earnings_calendar_event_id == event.id
    assert snapshot.benchmark_portfolio_id == portfolio.id
    assert snapshot.ticker == "ZZFRZ"
    assert snapshot.company_name == event.company_name
    assert snapshot.strategy_direction == DecisionDirection.BULLISH
    assert snapshot.strategy_type == StrategyCategory.LONG_CALL.value
    assert snapshot.generated_at == result.generated_at
    assert snapshot.underlying_price == UNDERLYING
    assert snapshot.selected_expiration == EXP
    assert snapshot.strategy_score == result.recommended.ranked.score.total
    assert snapshot.score_breakdown == result.recommended.ranked.score.as_dict()
    assert snapshot.legs == [
        {
            "option_type": leg.option_type.value,
            "action": leg.action.value,
            "strike": str(leg.strike),
            "premium": str(leg.premium),
            "quantity": leg.quantity,
            "multiplier": "100",
            "expiration": EXP.isoformat(),
            "external_contract_id": None,
        }
        for leg in result.recommended.ranked.candidate.legs
    ]
    assert snapshot.why_this_strategy == ["real reason"]
    assert snapshot.why_this_expiration == ["real expiration reason"]
    assert snapshot.why_not_alternatives == ["real why-not reason"]
    assert snapshot.engine_version == ENGINE_VERSION
    assert snapshot.prompt_version == PROMPT_VERSION
    assert snapshot.expiration_source == EXPIRATION_SOURCE_V3_RESOLVER


def test_freeze_decision_snapshot_handles_no_recommended_strategy(db_session):
    """generate_decision() can genuinely return no recommended strategy
    (no actionable real market data) -- a real, honest, frozen outcome,
    never guessed or backfilled (mirrors ai_decision_version's own
    precedent for this exact scenario)."""
    company = _seed_company(db_session, ticker="ZZFRZ2")
    event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZ2")
    result = _decision_result(recommended=None)

    snapshot = freeze_decision_snapshot(
        db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
    )
    db_session.flush()

    assert snapshot.id is not None
    assert snapshot.strategy_type is None
    assert snapshot.legs is None
    assert snapshot.strategy_score is None
    assert snapshot.estimated_probability is None
    assert snapshot.why_this_strategy is None
    # header fields are still real and frozen regardless
    assert snapshot.strategy_direction == DecisionDirection.BULLISH


def test_freeze_decision_snapshot_freezes_probability(db_session):
    """The historical_compatibility/estimated_probability computed here
    must match assess_move_compatibility's real math over a real
    historical sample -- frozen once, not recomputed live (Phase 4.3
    decision #2)."""
    company = _seed_company(db_session, ticker="ZZFRZ3")
    event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZ3")

    # A real historical earnings event whose next-day move (12%) is well
    # beyond the long call's breakeven distance -- a real "compatible"
    # data point for a debit position.
    historical_event = EarningsEvent(company_id=company.id, fiscal_year=2025, fiscal_quarter=3)
    db_session.add(historical_event)
    db_session.flush()
    db_session.add(
        PriceReaction(
            earnings_event_id=historical_event.id,
            next_day_move_pct=Decimal("0.12"),
            source_provider="test",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    result = _decision_result()
    snapshot = freeze_decision_snapshot(
        db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
    )
    db_session.flush()

    assert snapshot.historical_sample_size == 1
    assert snapshot.historical_compatibility is not None
    assert snapshot.historical_compatibility["compatible_count"] == 1
    assert snapshot.estimated_probability is not None
    assert snapshot.confidence_interval is not None
    assert snapshot.confidence_interval["low_sample_confidence"] is True


def test_freeze_decision_snapshot_uses_volatility_snapshot_for_market_fields(db_session):
    company = _seed_company(db_session, ticker="ZZFRZ4")
    event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZ4")

    vol_snapshot = VolatilitySnapshot(
        company_id=company.id,
        snapshot_timestamp=datetime.now(UTC),
        method="test",
        atm_iv_near=Decimal("0.45"),
        iv_percentile=Decimal("82"),
        computed_at=datetime.now(UTC),
    )
    db_session.add(vol_snapshot)
    db_session.flush()

    result = _decision_result(volatility_snapshot_id=vol_snapshot.id)
    snapshot = freeze_decision_snapshot(
        db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
    )
    db_session.flush()

    assert snapshot.option_snapshot_reference == vol_snapshot.id
    assert snapshot.implied_volatility == Decimal("0.45")
    assert snapshot.volatility_regime == "high"


def test_classify_volatility_regime():
    assert _classify_volatility_regime(None) is None
    assert _classify_volatility_regime(Decimal("85")) == "high"
    assert _classify_volatility_regime(Decimal("70")) == "high"
    assert _classify_volatility_regime(Decimal("50")) == "normal"
    assert _classify_volatility_regime(Decimal("30")) == "low"
    assert _classify_volatility_regime(Decimal("10")) == "low"


class TestReproducibilityFieldsFrozen:
    """Phase 4 reproducibility hardening (2026-08-26), Section 42 -- a
    future DecisionSnapshot must freeze enough of DecisionResult to
    reconstruct WHY the system decided what it did, even though these
    inputs were already computed by generate_decision() well before this
    phase existed to freeze them."""

    def test_volatility_view_is_frozen(self, db_session):
        company = _seed_company(db_session, ticker="ZZFRZ5")
        event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZ5")
        result = _decision_result(view=_decision_view())  # volatility_view="long_vol"

        snapshot = freeze_decision_snapshot(
            db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
        )
        db_session.flush()

        assert snapshot.volatility_view == DecisionVolatilityView.LONG_VOL

    def test_effective_risk_profile_is_frozen_independent_of_portfolio(self, db_session):
        """The frozen value must come from DecisionResult.risk_profile (the
        profile actually used to generate this decision), not from
        portfolio.risk_profile -- which is mutable and must never change
        how an old decision is interpreted."""
        company = _seed_company(db_session, ticker="ZZFRZ6")
        event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZ6")
        assert portfolio.risk_profile == RiskProfile.MODERATE  # the model default
        result = _decision_result(risk_profile=RiskProfile.AGGRESSIVE)

        snapshot = freeze_decision_snapshot(
            db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
        )
        db_session.flush()

        assert snapshot.effective_risk_profile == RiskProfile.AGGRESSIVE

        # Later mutation of the portfolio's own risk_profile must never
        # retroactively change what this already-frozen snapshot says.
        portfolio.risk_profile = RiskProfile.CONSERVATIVE
        db_session.flush()
        db_session.refresh(snapshot)
        assert snapshot.effective_risk_profile == RiskProfile.AGGRESSIVE

    def test_deterministic_confidence_is_frozen_with_breakdown(self, db_session):
        company = _seed_company(db_session, ticker="ZZFRZ7")
        event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZ7")
        confidence = ConfidenceComponents(
            evidence_coverage=25,
            consensus_agreement=15,
            historical_consistency=20,
            data_freshness=10,
            options_completeness=20,
        )
        result = _decision_result(confidence=confidence)

        snapshot = freeze_decision_snapshot(
            db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
        )
        db_session.flush()

        assert snapshot.deterministic_confidence_score == confidence.total == 90
        assert snapshot.deterministic_confidence_breakdown == confidence.as_dict()

    def test_decision_llm_identity_is_frozen(self, db_session):
        company = _seed_company(db_session, ticker="ZZFRZ8")
        event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZ8")
        result = _decision_result(provider="deepseek", model="deepseek-chat")

        snapshot = freeze_decision_snapshot(
            db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
        )
        db_session.flush()

        assert snapshot.decision_llm_provider == "deepseek"
        assert snapshot.decision_llm_model == "deepseek-chat"

    def test_external_contract_id_frozen_when_known_at_decision_time(self, db_session):
        """Section 7 -- when the real quotes candidate generation used
        carry a real external_contract_id, the matching leg's frozen dict
        must preserve it, matched by (strike, option_type)."""
        company = _seed_company(db_session, ticker="ZZFRZ9")
        event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZ9")
        quote = OptionQuote(
            ticker="ZZFRZ9",
            snapshot_timestamp=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            expiration_date=EXP,
            strike=Decimal("100"),
            option_type="call",
            bid=Decimal("1.9"),
            ask=Decimal("2.1"),
            source_provider="ibkr",
            retrieved_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            external_contract_id="712345678",
        )
        result = _decision_result(option_quotes=[quote])

        snapshot = freeze_decision_snapshot(
            db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
        )
        db_session.flush()

        assert snapshot.legs[0]["external_contract_id"] == "712345678"
        assert snapshot.legs[0]["expiration"] == EXP.isoformat()

    def test_external_contract_id_null_when_not_known_at_decision_time(self, db_session):
        """Point-in-time honesty (Section 7/16): a leg whose quote never
        carried a real external_contract_id must freeze None, never a
        later re-resolution pretending it was known at generation time."""
        company = _seed_company(db_session, ticker="ZZFRZA")
        event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZA")
        # A quote for the exact same contract, but with no contract id at
        # all -- e.g. a provider with no such concept (providers/types.py's
        # own external_contract_id docstring).
        quote = OptionQuote(
            ticker="ZZFRZA",
            snapshot_timestamp=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            expiration_date=EXP,
            strike=Decimal("100"),
            option_type="call",
            bid=Decimal("1.9"),
            ask=Decimal("2.1"),
            source_provider="alpha_vantage",
            retrieved_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            external_contract_id=None,
        )
        result = _decision_result(option_quotes=[quote])

        snapshot = freeze_decision_snapshot(
            db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
        )
        db_session.flush()

        assert snapshot.legs[0]["external_contract_id"] is None

    def test_external_contract_id_matched_by_expiration_not_just_strike(self, db_session):
        """Post-official-run cleanup (2026-08-27), Section 5 -- a real
        snapshot can carry the same strike/right at more than one
        expiration (services/options_analytics.py::select_pricing_
        snapshot never filters its rows to one expiration; that's exactly
        how term-structure computation gets a second expiration's quotes
        from the same call). The frozen leg must pick the contract id
        from result.expiration -- the expiration the recommended strategy
        actually uses -- never the other expiration's contract, even
        though both share (strike, option_type)."""
        company = _seed_company(db_session, ticker="ZZFRZB")
        event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZB")
        other_expiration = date(2026, 9, 25)
        correct_quote = OptionQuote(
            ticker="ZZFRZB",
            snapshot_timestamp=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            expiration_date=EXP,
            strike=Decimal("100"),
            option_type="call",
            bid=Decimal("1.9"),
            ask=Decimal("2.1"),
            source_provider="ibkr",
            retrieved_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            external_contract_id="712345678",
        )
        wrong_expiration_quote = OptionQuote(
            ticker="ZZFRZB",
            snapshot_timestamp=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            expiration_date=other_expiration,
            strike=Decimal("100"),
            option_type="call",
            bid=Decimal("2.4"),
            ask=Decimal("2.6"),
            source_provider="ibkr",
            retrieved_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            external_contract_id="999999999",
        )
        result = _decision_result(option_quotes=[wrong_expiration_quote, correct_quote])

        snapshot = freeze_decision_snapshot(
            db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
        )
        db_session.flush()

        assert snapshot.legs[0]["external_contract_id"] == "712345678"
        assert snapshot.legs[0]["expiration"] == EXP.isoformat()

    def test_same_key_collision_keeps_first_seen_without_raising(self, db_session):
        """A genuine same (expiration, strike, option_type) collision with
        two different real contract ids would mean the snapshot itself is
        corrupt -- never observed in real data. Freezing must stay
        deterministic (first one seen) and must never turn this into a
        new way for decision generation to fail."""
        company = _seed_company(db_session, ticker="ZZFRZC")
        event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZC")
        first_quote = OptionQuote(
            ticker="ZZFRZC",
            snapshot_timestamp=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            expiration_date=EXP,
            strike=Decimal("100"),
            option_type="call",
            bid=Decimal("1.9"),
            ask=Decimal("2.1"),
            source_provider="ibkr",
            retrieved_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            external_contract_id="111111111",
        )
        duplicate_quote = OptionQuote(
            ticker="ZZFRZC",
            snapshot_timestamp=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            expiration_date=EXP,
            strike=Decimal("100"),
            option_type="call",
            bid=Decimal("1.9"),
            ask=Decimal("2.1"),
            source_provider="ibkr",
            retrieved_at=datetime(2026, 9, 16, 15, 55, tzinfo=UTC),
            external_contract_id="222222222",
        )
        result = _decision_result(option_quotes=[first_quote, duplicate_quote])

        snapshot = freeze_decision_snapshot(
            db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
        )
        db_session.flush()

        assert snapshot.legs[0]["external_contract_id"] == "111111111"

    def test_no_recommended_strategy_still_freezes_pre_strategy_inputs(self, db_session):
        """volatility_view/effective_risk_profile/confidence/LLM identity
        are all computed before strategy candidate generation even runs --
        a decision with no recommended strategy still has real values for
        these, unlike strategy_score/legs which are genuinely None."""
        company = _seed_company(db_session, ticker="ZZFRZB")
        event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZB")
        result = _decision_result(recommended=None)

        snapshot = freeze_decision_snapshot(
            db_session, calendar_event=event, portfolio=portfolio, company=company, result=result
        )
        db_session.flush()

        assert snapshot.volatility_view == DecisionVolatilityView.LONG_VOL
        assert snapshot.effective_risk_profile == RiskProfile.MODERATE
        assert snapshot.deterministic_confidence_score is not None
        assert snapshot.decision_llm_provider == "deepseek"
        assert snapshot.strategy_score is None
        assert snapshot.legs is None


def test_historical_fixture_with_reproducibility_fields_null_still_loads(db_session):
    """Section 42 -- a legacy row (Aug 25 or any pre-hardening snapshot)
    that never had these columns populated must still load correctly:
    a plain, real DecisionSnapshot insert that never sets the six new
    columns at all, exactly like an old row would look after the
    additive migration (see migrations/versions/7125ac88b7c7_*)."""
    from models.decision_snapshot import DecisionSnapshot

    _seed_company(db_session, ticker="ZZFRZC")
    event, portfolio = _seed_event_and_portfolio(db_session, symbol="ZZFRZC")

    legacy_row = DecisionSnapshot(
        earnings_calendar_event_id=event.id,
        benchmark_portfolio_id=portfolio.id,
        ticker="ZZFRZC",
        company_name="ZZ Freeze Test Co",
        strategy_direction=DecisionDirection.BULLISH,
        generated_at=datetime(2026, 8, 25, 19, 55, tzinfo=UTC),
        engine_version=ENGINE_VERSION,
        prompt_version=PROMPT_VERSION,
        expiration_source=EXPIRATION_SOURCE_V3_RESOLVER,
    )
    db_session.add(legacy_row)
    db_session.flush()
    db_session.refresh(legacy_row)

    assert legacy_row.id is not None
    assert legacy_row.volatility_view is None
    assert legacy_row.effective_risk_profile is None
    assert legacy_row.deterministic_confidence_score is None
    assert legacy_row.deterministic_confidence_breakdown is None
    assert legacy_row.decision_llm_provider is None
    assert legacy_row.decision_llm_model is None
