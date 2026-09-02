"""Options Decision Engine V4.2/V4.3/V4.4A -- experimental, read-only
diagnostic endpoints for (1) view <-> strategy semantic compatibility
(analytics/decision/v4_compatibility.py), (2) expected-move-aware
strike selection (analytics/decision/v4_strike_engine.py), and (3) T+1
scenario valuation (analytics/decision/v4_t1_pricing.py).

NOT AN OFFICIAL TRADING SURFACE. Every endpoint is pure computation
over caller-supplied inputs -- never a recommendation, never touches
any real DecisionSnapshot/EntryCaptureAttempt/EntrySnapshot, never
calls an LLM or a market-data provider. The strike-selection and T+1
scenario-valuation endpoints run against a SYNTHETIC chain/leg built
from the query parameters (see ``_synthetic_chain``/
``get_v4_t1_scenario_valuation`` below) -- neither reads a real
captured quote, so their output is a mechanics demonstration, not a
tradeable candidate.

Disabled by default in any production deployment, two layers deep --
api/main.py only registers this router at all when app_env != production
(so a production deployment's /docs doesn't even list these routes,
mirroring api/routers/admin.py's own established pattern exactly), and
this router's own _ensure_enabled (same helper, same call convention as
admin.py's) checks settings.app_env again as defense in depth.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from fastapi import APIRouter

from analytics.decision.v4_compatibility import evaluate_semantic_compatibility
from analytics.decision.v4_expected_move import EXPECTED_MOVE_CONTEXT_VERSION, ExpectedMoveContext
from analytics.decision.v4_market_view import derive_v4_market_view
from analytics.decision.v4_strategy_semantics import get_strategy_semantics
from analytics.decision.v4_strike_engine import select_v4_strikes
from analytics.decision.v4_strike_resolver import Right
from analytics.decision.v4_t1_pricing import (
    FrictionLevel,
    evaluate_candidate_t1_scenarios,
    summarize_candidate_distribution,
)
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from analytics.options.strategy_candidates import StrategyCategory
from api.exceptions import NotFoundError
from core.config import Settings, get_settings
from models.enums import DecisionDirection, DecisionVolatilityView
from providers.types import OptionQuote
from schemas.api import (
    V4CompatibilityResponse,
    V4StrikeLegResponse,
    V4StrikeSelectionResponse,
    V4T1ScenarioPointResponse,
    V4T1ScenarioValuationResponse,
)

router = APIRouter(prefix="/v4/experimental", tags=["v4-experimental"])


def _ensure_enabled(settings: Settings) -> None:
    if settings.app_env.lower() == "production":
        raise NotFoundError("not found")


@router.get("/compatibility", response_model=V4CompatibilityResponse)
def get_v4_compatibility(
    direction: DecisionDirection,
    strategy: StrategyCategory,
    volatility_view: DecisionVolatilityView | None = None,
) -> V4CompatibilityResponse:
    settings = get_settings()
    _ensure_enabled(settings)

    market_view = derive_v4_market_view(direction, volatility_view)
    semantics = get_strategy_semantics(strategy)
    result = evaluate_semantic_compatibility(market_view, semantics)
    return V4CompatibilityResponse(
        direction=direction.value,
        volatility_view=volatility_view.value if volatility_view else None,
        expected_move_intent=market_view.expected_move_intent,
        strategy=strategy.value,
        direction_compatibility=result.direction_compatibility,
        move_magnitude_compatibility=result.move_magnitude_compatibility,
        volatility_compatibility=result.volatility_compatibility,
        payoff_shape_compatibility=result.payoff_shape_compatibility,
        overall_semantic_compatibility=result.overall_semantic_compatibility,
        tier=result.tier,
        reason_codes=list(result.reason_codes),
        explanation=result.explanation,
    )


def _synthetic_chain(
    spot: Decimal, strike_spacing: Decimal, steps_each_side: int = 30
) -> list[OptionQuote]:
    """A SYNTHETIC listed-strike chain for demonstration only -- real
    strikes spaced ``strike_spacing`` apart around ``spot``, each with
    a nominal, non-market-derived quote. Never a real captured chain;
    every quote's ``source_provider`` is labeled ``"synthetic"`` so
    this can never be confused with real market data downstream."""
    now = datetime.now(UTC)
    quotes = []
    for i in range(-steps_each_side, steps_each_side + 1):
        strike = spot + strike_spacing * Decimal(i)
        if strike <= 0:
            continue
        for right in ("call", "put"):
            quotes.append(
                OptionQuote(
                    source_provider="synthetic",
                    retrieved_at=now,
                    ticker="SYNTHETIC",
                    snapshot_timestamp=now,
                    expiration_date=now.date(),
                    strike=strike,
                    option_type=right,
                    bid=Decimal("0.95"),
                    ask=Decimal("1.05"),
                )
            )
    return quotes


def _synthetic_context(
    spot: Decimal,
    implied_move_pct: Decimal | None,
    historical_median_abs_move_pct: Decimal | None,
) -> ExpectedMoveContext:
    """A caller-specified ExpectedMoveContext -- bypasses
    derive_expected_move_context entirely since this endpoint has no
    real quotes/history to derive from, only the query parameters
    themselves. ``historical_evidence_quality`` is always "limited"
    when a median is supplied (a single caller-given number, never a
    real sample) or "insufficient" otherwise -- never claims a false
    "adequate" tier this endpoint has no real sample size to back."""
    implied_available = implied_move_pct is not None
    implied_dollars = spot * implied_move_pct if implied_move_pct is not None else None
    hist_median = historical_median_abs_move_pct
    return ExpectedMoveContext(
        spot=spot,
        observed_at=datetime.now(UTC),
        implied_move_available=implied_available,
        implied_move_dollars=implied_dollars,
        implied_move_pct=implied_move_pct,
        upper_implied_boundary=(spot + implied_dollars) if implied_dollars is not None else None,
        lower_implied_boundary=(spot - implied_dollars) if implied_dollars is not None else None,
        implied_move_source="atm_straddle" if implied_available else "unavailable",
        implied_move_result=None,
        historical_sample_n=0,
        historical_evidence_quality="limited" if hist_median is not None else "insufficient",
        historical_median_abs_move_pct=hist_median,
        historical_median_upper_boundary=(spot * (1 + hist_median))
        if hist_median is not None
        else None,
        historical_median_lower_boundary=(spot * (1 - hist_median))
        if hist_median is not None
        else None,
        historical_quantiles=None,
        historical_move_stats=None,
        context_version=EXPECTED_MOVE_CONTEXT_VERSION,
    )


@router.get("/strike-selection", response_model=V4StrikeSelectionResponse)
def get_v4_strike_selection(
    strategy: StrategyCategory,
    spot: Decimal,
    implied_move_pct: Decimal | None = None,
    historical_median_abs_move_pct: Decimal | None = None,
    strike_spacing: Decimal = Decimal("1"),
) -> V4StrikeSelectionResponse:
    """Runs analytics/decision/v4_strike_engine.py against a SYNTHETIC
    chain (see ``_synthetic_chain``) built purely from these query
    parameters -- a geometry demonstration, never a real trading
    candidate; no real quote is ever read."""
    settings = get_settings()
    _ensure_enabled(settings)

    quotes = _synthetic_chain(spot, strike_spacing)
    context = _synthetic_context(spot, implied_move_pct, historical_median_abs_move_pct)
    result = select_v4_strikes(strategy, context, quotes)

    return V4StrikeSelectionResponse(
        strategy=result.strategy.value,
        status=result.status,
        spot=result.spot,
        implied_move_dollars=context.implied_move_dollars,
        implied_move_pct=context.implied_move_pct,
        historical_median_abs_move_pct=context.historical_median_abs_move_pct,
        legs=[
            V4StrikeLegResponse(
                action=leg.action,
                right=leg.right,
                quantity=leg.quantity,
                target_price=leg.target_price,
                target_rationale=leg.target_rationale,
                selected_strike=leg.selected_strike,
                target_distance_dollars=leg.target_distance_dollars,
                target_distance_pct=leg.target_distance_pct,
                moneyness_pct=leg.moneyness_pct,
                expected_move_units=leg.expected_move_units,
                external_contract_id=leg.external_contract_id,
                quote_quality=leg.quote_quality,
                spread_pct=leg.spread_pct,
                volume=leg.volume,
                open_interest=leg.open_interest,
                reason_codes=list(leg.reason_codes),
            )
            for leg in result.legs
        ],
        center_target=result.center_target,
        lower_boundary=result.lower_boundary,
        upper_boundary=result.upper_boundary,
        width=result.width,
        width_pct_of_spot=result.width_pct_of_spot,
        width_in_expected_move_units=result.width_in_expected_move_units,
        symmetry_error_pct=result.symmetry_error_pct,
        reason_codes=list(result.reason_codes),
        explanation=result.explanation,
        engine_version=result.engine_version,
    )


@router.get("/t1-scenario-valuation", response_model=V4T1ScenarioValuationResponse)
def get_v4_t1_scenario_valuation(
    strategy: StrategyCategory,
    right: Right,
    action: Literal["buy", "sell"],
    spot: Decimal,
    strike: Decimal,
    entry_bid: Decimal,
    entry_ask: Decimal,
    entry_iv: Decimal,
    dte_entry: int,
    implied_move_pct: Decimal | None = None,
    historical_median_abs_move_pct: Decimal | None = None,
    friction_level: FrictionLevel = "NORMAL_FRICTION",
) -> V4T1ScenarioValuationResponse:
    """EXPERIMENTAL. MODEL-BASED T+1 SCENARIOS ONLY -- NOT AN OFFICIAL
    RECOMMENDATION. Runs analytics/decision/v4_t1_pricing.py against
    ONE synthetic leg built purely from these query parameters -- a
    scenario-mechanics demonstration, never a real trading candidate;
    no real quote is ever read, no DecisionSnapshot is ever written.
    ``entry_bid``/``entry_ask`` and ``entry_iv`` are the caller's own
    hypothetical inputs, not a live quote lookup."""
    settings = get_settings()
    _ensure_enabled(settings)

    now = datetime.now(UTC)
    expiration = now.date() + timedelta(days=max(dte_entry, 1))
    exit_timestamp = now + timedelta(days=1)

    leg = V4T1LegInput(
        leg_index=0,
        action=action,
        right=right,
        strike=strike,
        quantity=1,
        multiplier=Decimal("100"),
        entry_bid=entry_bid,
        entry_ask=entry_ask,
        entry_last=None,
        entry_iv=entry_iv,
        entry_delta=None,
        entry_gamma=None,
        entry_theta=None,
        entry_vega=None,
        market_data_quality="synthetic",
        external_contract_id=None,
    )
    expected_move_context = _synthetic_context(
        spot, implied_move_pct, historical_median_abs_move_pct
    )
    valuation_context = V4T1ValuationContext(
        ticker="SYNTHETIC",
        underlying_price=spot,
        observed_at=now,
        entry_timestamp=now,
        expected_exit_timestamp=exit_timestamp,
        strategy=strategy,
        expiration=expiration,
        legs=(leg,),
        expected_move_context=expected_move_context,
    )

    results = evaluate_candidate_t1_scenarios(valuation_context, "diagnostic", friction_level)
    if results is None:
        return V4T1ScenarioValuationResponse(
            strategy=strategy.value,
            entry_cashflow=None,
            scenarios=[],
            n_scenarios=0,
            n_valued=0,
            min_return=None,
            max_return=None,
            median_return=None,
            scenario_average_return=None,
            positive_scenario_fraction=None,
            quality_note=(
                "Neither implied_move_pct nor historical_median_abs_move_pct was supplied -- "
                "cannot build the underlying-move scenario grid."
            ),
            engine_version="t1_pricing_v1",
        )

    summary = summarize_candidate_distribution(results)
    entry_cashflow = results[0].entry_cashflow if results else None
    return V4T1ScenarioValuationResponse(
        strategy=strategy.value,
        entry_cashflow=entry_cashflow,
        scenarios=[
            V4T1ScenarioPointResponse(
                scenario_id=r.scenario_id,
                underlying_move_label=r.underlying_move_label,
                scenario_underlying_price=r.scenario_underlying_price,
                iv_scenario_label=r.iv_scenario_label,
                theoretical_liquidation_value=r.theoretical_liquidation_value,
                executable_liquidation_value=r.executable_liquidation_value,
                realized_equivalent_pnl_theoretical=r.realized_equivalent_pnl_theoretical,
                realized_equivalent_pnl_executable=r.realized_equivalent_pnl_executable,
                return_on_standardized_capital_theoretical=r.return_on_standardized_capital_theoretical,
                return_on_standardized_capital_executable=r.return_on_standardized_capital_executable,
                reason_codes=list(r.reason_codes),
            )
            for r in results
        ],
        n_scenarios=summary.n_scenarios,
        n_valued=summary.n_valued,
        min_return=summary.min_return,
        max_return=summary.max_return,
        median_return=summary.median_return,
        scenario_average_return=summary.scenario_average_return,
        positive_scenario_fraction=summary.positive_scenario_fraction,
        quality_note=summary.quality_note,
        engine_version="t1_pricing_v1",
    )
