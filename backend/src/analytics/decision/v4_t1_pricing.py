"""Options Decision Engine V4.4A -- T+1 Option Repricing & Scenario
Valuation (2026-09-03).

Answers this task's own stated question: "for one already-defined
option candidate, what does its economic outcome distribution look
like at the actual official T+1 exit horizon?" Never a final score,
never a rank, never a winner (Section 1) -- every result here is an
orthogonal measurement V4.4B will later combine with V4.2's semantic
compatibility and V4.4A's own liquidity diagnostics.

REUSES THE REAL, ALREADY-TESTED BLACK-SCHOLES KERNEL
(analytics/options/black_scholes.py::price_and_greeks) rather than
building a second pricing implementation -- confirmed by direct audit
to be a pure, generic function with zero reconstruction-specific
coupling, previously called only from services/options_reconstruction
.py's historical-close path. This module is its second real caller.

THEORETICAL VALUE IS NOT AN EXECUTABLE QUOTE (Section 13, mandatory).
Every scenario computes BOTH a model theoretical price (raw
Black-Scholes output) AND a separate estimated executable exit price
(the model price treated as a proxy mid, discounted to the bid side for
closing a long, premium'd to the ask side for closing a short, per a
real, disclosed spread-% assumption) -- kept as permanently separate
fields, never conflated.

RISK-FREE RATE: redefines, rather than imports, the exact same
``Decimal("0.04")`` value already established in
``services/options_reconstruction.py::_risk_free_rate_assumption``
(that function is private to a reconstruction-specific module this
task does not touch) -- same documented assumption, zero cross-module
coupling to a private helper.

DIVIDEND YIELD: 0.0, matching every real caller of ``price_and_greeks``
in this codebase today -- confirmed by audit that no real dividend-
yield data source exists anywhere.

EXECUTION-FRICTION LEVELS are real, disclosed p25/p50/p75 spread-%-of-
mid quantiles computed directly from ``options_snapshot`` (n=700 real
rows with a priceable bid and ask) -- not invented, not fit to any
trade's realized P&L (spread% is a market-microstructure fact,
independent of outcome).
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from analytics.decision.v4_capital import PER_DECISION_CAPITAL
from analytics.decision.v4_strike_resolver import Right
from analytics.decision.v4_t1_scenario_grid import (  # noqa: F401  (IVScenario re-exported for callers)
    IVScenario,
    UnderlyingScenario,
    build_iv_scenarios,
    build_underlying_scenarios,
    scenario_leg_iv,
)
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from analytics.options.black_scholes import price_and_greeks
from analytics.options.payoff import Action, OptionLeg, analyze
from models.enums import OptionType

T1_PRICING_VERSION = "t1_pricing_v1"

# Matches services/options_reconstruction.py::_risk_free_rate_assumption's
# own documented value exactly -- see this module's own docstring for
# why it is redefined here rather than imported.
T1_RISK_FREE_RATE_ASSUMPTION = Decimal("0.04")
T1_DIVIDEND_YIELD_ASSUMPTION = Decimal("0")

FrictionLevel = Literal["LOW_FRICTION", "NORMAL_FRICTION", "HIGH_FRICTION"]

# Section 14 -- real p25/p50/p75 spread-as-%-of-mid quantiles, computed
# directly from options_snapshot (n=700 real rows, bid/ask both real
# and positive). See the V4.4A report's own audit section for the
# exact query.
EXECUTION_FRICTION_SPREAD_PCT: dict[FrictionLevel, Decimal] = {
    "LOW_FRICTION": Decimal("0.04"),
    "NORMAL_FRICTION": Decimal("0.10"),
    "HIGH_FRICTION": Decimal("0.18"),
}
EXECUTION_FRICTION_EVIDENCE_NOTE = (
    "LOW/NORMAL/HIGH friction = real p25/p50/p75 bid-ask spread-as-%-of-mid quantiles from "
    "options_snapshot (n=700 real rows with a priceable bid and ask) -- not invented, not fit "
    "to any trade's realized P&L."
)

NO_ENTRY_IV = "NO_ENTRY_IV"
PRICING_ERROR = "PRICING_ERROR"
MISSING_ENTRY_PRICE = "MISSING_ENTRY_PRICE"


# --------------------------------------------------------------------------
# Per-leg repricing (Sections 5, 12, 13, 15).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class T1LegScenarioValue:
    leg_index: int
    action: Literal["buy", "sell"]
    right: Right
    strike: Decimal
    quantity: int
    multiplier: Decimal
    scenario_iv: Decimal | None
    model_price: Decimal | None
    model_delta: Decimal | None
    model_gamma: Decimal | None
    model_theta: Decimal | None
    model_vega: Decimal | None
    executable_exit_price: Decimal | None
    execution_friction_method: str
    entry_market_data_quality: str | None
    reason_codes: tuple[str, ...]


def _executable_exit_price(
    model_price: Decimal, action: Literal["buy", "sell"], friction_level: FrictionLevel
) -> Decimal:
    """Section 13/15 -- the model price is treated as a proxy mid.
    Closing a LONG (action=="buy" at entry) means selling now -> the
    BID side, a discount below the model mid. Closing a SHORT
    (action=="sell" at entry) means buying now -> the ASK side, a
    premium above the model mid. Half the disclosed spread% on each
    side of the model 'mid', matching the same spread-%-of-mid formula
    services/options_reconstruction.py::classify_chain_quality already
    uses."""
    half_spread = EXECUTION_FRICTION_SPREAD_PCT[friction_level] / 2
    if action == "buy":
        executable = model_price * (1 - half_spread)
    else:
        executable = model_price * (1 + half_spread)
    return max(executable, Decimal(0))


def price_leg_at_scenario(
    leg: V4T1LegInput,
    scenario_underlying_price: Decimal,
    iv_scenario: IVScenario,
    dte_exit_for_pricing: int,
    friction_level: FrictionLevel,
) -> T1LegScenarioValue:
    """Pure, deterministic (Section 40): same inputs -> identical
    output, every time. No DB access, no live call, no LLM."""
    scenario_iv = scenario_leg_iv(leg.entry_iv, iv_scenario)
    friction_method = (
        f"model price at proxy mid, {EXECUTION_FRICTION_SPREAD_PCT[friction_level]:.0%} "
        f"{friction_level} spread halved to the {'bid' if leg.action == 'buy' else 'ask'} side"
    )
    if scenario_iv is None or scenario_iv <= 0:
        return T1LegScenarioValue(
            leg_index=leg.leg_index,
            action=leg.action,
            right=leg.right,
            strike=leg.strike,
            quantity=leg.quantity,
            multiplier=leg.multiplier,
            scenario_iv=scenario_iv,
            model_price=None,
            model_delta=None,
            model_gamma=None,
            model_theta=None,
            model_vega=None,
            executable_exit_price=None,
            execution_friction_method=friction_method,
            entry_market_data_quality=leg.market_data_quality,
            reason_codes=(NO_ENTRY_IV,),
        )
    try:
        greeks = price_and_greeks(
            OptionType(leg.right),
            float(scenario_underlying_price),
            float(leg.strike),
            float(Decimal(dte_exit_for_pricing) / Decimal(365)),
            float(T1_RISK_FREE_RATE_ASSUMPTION),
            float(scenario_iv),
            float(T1_DIVIDEND_YIELD_ASSUMPTION),
        )
    except ValueError:
        return T1LegScenarioValue(
            leg_index=leg.leg_index,
            action=leg.action,
            right=leg.right,
            strike=leg.strike,
            quantity=leg.quantity,
            multiplier=leg.multiplier,
            scenario_iv=scenario_iv,
            model_price=None,
            model_delta=None,
            model_gamma=None,
            model_theta=None,
            model_vega=None,
            executable_exit_price=None,
            execution_friction_method=friction_method,
            entry_market_data_quality=leg.market_data_quality,
            reason_codes=(PRICING_ERROR,),
        )
    model_price = max(Decimal(str(greeks.price)), Decimal(0))
    executable = _executable_exit_price(model_price, leg.action, friction_level)
    return T1LegScenarioValue(
        leg_index=leg.leg_index,
        action=leg.action,
        right=leg.right,
        strike=leg.strike,
        quantity=leg.quantity,
        multiplier=leg.multiplier,
        scenario_iv=scenario_iv,
        model_price=model_price,
        model_delta=Decimal(str(greeks.delta)),
        model_gamma=Decimal(str(greeks.gamma)),
        model_theta=Decimal(str(greeks.theta)),
        model_vega=Decimal(str(greeks.vega)),
        executable_exit_price=executable,
        execution_friction_method=friction_method,
        entry_market_data_quality=leg.market_data_quality,
        reason_codes=(),
    )


# --------------------------------------------------------------------------
# Strategy-level per-scenario result (Sections 17, 18).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class T1ScenarioResult:
    variant_id: str
    scenario_id: str
    underlying_move_label: str
    underlying_move_em_fraction: Decimal
    scenario_underlying_price: Decimal
    iv_scenario_label: str
    iv_scenario_multiplier: Decimal
    dte_remaining_at_exit: int
    leg_values: tuple[T1LegScenarioValue, ...]
    entry_cashflow: Decimal | None
    theoretical_liquidation_value: Decimal | None
    executable_liquidation_value: Decimal | None
    realized_equivalent_pnl_theoretical: Decimal | None
    realized_equivalent_pnl_executable: Decimal | None
    return_on_standardized_capital_theoretical: Decimal | None
    return_on_standardized_capital_executable: Decimal | None
    return_on_entry_cash: Decimal | None
    reason_codes: tuple[str, ...]
    quality_note: str


def _entry_cashflow(legs: tuple[V4T1LegInput, ...]) -> tuple[Decimal | None, tuple[str, ...]]:
    """Section 5 -- built entirely from executable entry sides (ASK for
    buy, BID for sell), never a midpoint."""
    total = Decimal(0)
    for leg in legs:
        price = leg.entry_executable_price
        if price is None:
            return None, (f"LEG_{leg.leg_index}_{MISSING_ENTRY_PRICE}",)
        sign = -1 if leg.action == "buy" else 1
        total += sign * price * leg.quantity * leg.multiplier
    return total, ()


def evaluate_candidate_t1_scenario(
    context: V4T1ValuationContext,
    underlying_scenario: UnderlyingScenario,
    iv_scenario: IVScenario,
    friction_level: FrictionLevel,
    variant_id: str,
) -> T1ScenarioResult:
    """Section 17 -- built from the actual leg structure, never an
    expiration intrinsic-value shortcut."""
    dte = context.dte_exit_for_pricing()
    scenario_id = f"{underlying_scenario.label}__{iv_scenario.label}"
    leg_values = tuple(
        price_leg_at_scenario(
            leg, underlying_scenario.scenario_underlying_price, iv_scenario, dte, friction_level
        )
        for leg in context.legs
    )
    entry_cashflow, entry_reason_codes = _entry_cashflow(context.legs)
    reason_codes: list[str] = list(entry_reason_codes)
    for leg_value in leg_values:
        reason_codes.extend(leg_value.reason_codes)

    incomplete = entry_cashflow is None or any(lv.model_price is None for lv in leg_values)
    if incomplete:
        return T1ScenarioResult(
            variant_id=variant_id,
            scenario_id=scenario_id,
            underlying_move_label=underlying_scenario.label,
            underlying_move_em_fraction=underlying_scenario.em_fraction,
            scenario_underlying_price=underlying_scenario.scenario_underlying_price,
            iv_scenario_label=iv_scenario.label,
            iv_scenario_multiplier=iv_scenario.multiplier,
            dte_remaining_at_exit=dte,
            leg_values=leg_values,
            entry_cashflow=entry_cashflow,
            theoretical_liquidation_value=None,
            executable_liquidation_value=None,
            realized_equivalent_pnl_theoretical=None,
            realized_equivalent_pnl_executable=None,
            return_on_standardized_capital_theoretical=None,
            return_on_standardized_capital_executable=None,
            return_on_entry_cash=None,
            reason_codes=tuple(dict.fromkeys(reason_codes)),
            quality_note="Scenario valuation incomplete -- see reason_codes; never fabricated.",
        )

    theoretical_liq = Decimal(0)
    executable_liq = Decimal(0)
    for leg, leg_value in zip(context.legs, leg_values, strict=True):
        exit_sign = 1 if leg.action == "buy" else -1
        assert leg_value.model_price is not None
        assert leg_value.executable_exit_price is not None
        theoretical_liq += exit_sign * leg_value.model_price * leg.quantity * leg.multiplier
        executable_liq += (
            exit_sign * leg_value.executable_exit_price * leg.quantity * leg.multiplier
        )

    assert entry_cashflow is not None
    pnl_theoretical = entry_cashflow + theoretical_liq
    pnl_executable = entry_cashflow + executable_liq
    return_on_cash = (pnl_executable / abs(entry_cashflow)) if entry_cashflow != 0 else None

    return T1ScenarioResult(
        variant_id=variant_id,
        scenario_id=scenario_id,
        underlying_move_label=underlying_scenario.label,
        underlying_move_em_fraction=underlying_scenario.em_fraction,
        scenario_underlying_price=underlying_scenario.scenario_underlying_price,
        iv_scenario_label=iv_scenario.label,
        iv_scenario_multiplier=iv_scenario.multiplier,
        dte_remaining_at_exit=dte,
        leg_values=leg_values,
        entry_cashflow=entry_cashflow,
        theoretical_liquidation_value=theoretical_liq,
        executable_liquidation_value=executable_liq,
        realized_equivalent_pnl_theoretical=pnl_theoretical,
        realized_equivalent_pnl_executable=pnl_executable,
        return_on_standardized_capital_theoretical=pnl_theoretical / PER_DECISION_CAPITAL,
        return_on_standardized_capital_executable=pnl_executable / PER_DECISION_CAPITAL,
        return_on_entry_cash=return_on_cash,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        quality_note="Scenario valuation complete.",
    )


def evaluate_candidate_t1_scenarios(
    context: V4T1ValuationContext,
    variant_id: str,
    friction_level: FrictionLevel = "NORMAL_FRICTION",
) -> tuple[T1ScenarioResult, ...] | None:
    """The one dispatcher every V4.4A caller uses -- Section 6's full
    scenario grid (7 underlying-move points x 3 IV-crush points = 21
    scenarios). Returns None only when the underlying-move grid itself
    can't be built (no implied move, no historical median -- an honest
    absence, Section 4). Pure computation (Section 40): no IBKR
    request is made anywhere in this loop -- every input is already a
    decision-time quote carried on ``context``."""
    underlying_scenarios = build_underlying_scenarios(context.expected_move_context)
    if underlying_scenarios is None:
        return None
    iv_scenarios = build_iv_scenarios()
    return tuple(
        evaluate_candidate_t1_scenario(context, us, ivs, friction_level, variant_id)
        for us in underlying_scenarios
        for ivs in iv_scenarios
    )


# --------------------------------------------------------------------------
# Candidate distribution summary (Sections 19, 20).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class T1CandidateDistributionSummary:
    variant_id: str
    n_scenarios: int
    n_valued: int
    min_return: Decimal | None
    max_return: Decimal | None
    median_return: Decimal | None
    lower_quartile_return: Decimal | None
    positive_scenario_fraction: Decimal | None
    scenario_average_return: Decimal | None
    weighted_expected_return: Decimal | None
    worst_scenario_id: str | None
    worst_scenario_return: Decimal | None
    quality_note: str


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _quantile(sorted_values: list[Decimal], q: Decimal) -> Decimal:
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    pos = q * (n - 1)
    lower_idx = int(pos)
    upper_idx = min(lower_idx + 1, n - 1)
    frac = pos - lower_idx
    return sorted_values[lower_idx] + (sorted_values[upper_idx] - sorted_values[lower_idx]) * frac


def summarize_candidate_distribution(
    results: tuple[T1ScenarioResult, ...],
    scenario_weights: dict[str, Decimal] | None = None,
) -> T1CandidateDistributionSummary:
    """Section 19's own mandatory terminology rule:
    ``scenario_average_return`` is an UNWEIGHTED arithmetic mean over
    the scenario grid, NEVER called "expected return" -- this codebase
    has no legitimate scenario probabilities (Section 8/37).
    ``weighted_expected_return`` is populated ONLY when a caller
    explicitly supplies real ``scenario_weights``; nothing in V4.4A
    itself ever does, since no legitimate weighting scheme exists yet."""
    n = len(results)
    variant_id = results[0].variant_id if results else "unknown"
    valued = [r for r in results if r.return_on_standardized_capital_executable is not None]
    if not valued:
        return T1CandidateDistributionSummary(
            variant_id=variant_id,
            n_scenarios=n,
            n_valued=0,
            min_return=None,
            max_return=None,
            median_return=None,
            lower_quartile_return=None,
            positive_scenario_fraction=None,
            scenario_average_return=None,
            weighted_expected_return=None,
            worst_scenario_id=None,
            worst_scenario_return=None,
            quality_note="No scenario could be valued -- insufficient decision-time evidence.",
        )
    returns = sorted(
        r.return_on_standardized_capital_executable
        for r in valued
        if r.return_on_standardized_capital_executable is not None
    )
    n_valued = len(returns)
    positive_fraction = Decimal(sum(1 for r in returns if r > 0)) / n_valued
    scenario_average = sum(returns, Decimal(0)) / n_valued
    worst = min(valued, key=lambda r: r.return_on_standardized_capital_executable or Decimal(0))

    weighted_expected = None
    quality_note = (
        f"{n_valued}/{n} scenarios valued. 'scenario_average_return' is an UNWEIGHTED mean over "
        "the scenario grid -- never called 'expected return' (no legitimate scenario "
        "probabilities exist, Section 19/8)."
    )
    if scenario_weights is not None:
        total_weight = sum(
            (scenario_weights.get(r.scenario_id, Decimal(0)) for r in valued), Decimal(0)
        )
        if total_weight > 0:
            weighted_expected = (
                sum(
                    (
                        scenario_weights.get(r.scenario_id, Decimal(0))
                        * (r.return_on_standardized_capital_executable or Decimal(0))
                        for r in valued
                    ),
                    Decimal(0),
                )
                / total_weight
            )
            quality_note += " Caller-supplied scenario_weights used for weighted_expected_return."

    return T1CandidateDistributionSummary(
        variant_id=variant_id,
        n_scenarios=n,
        n_valued=n_valued,
        min_return=returns[0],
        max_return=returns[-1],
        median_return=_median(returns),
        lower_quartile_return=_quantile(returns, Decimal("0.25")),
        positive_scenario_fraction=positive_fraction,
        scenario_average_return=scenario_average,
        weighted_expected_return=weighted_expected,
        worst_scenario_id=worst.scenario_id,
        worst_scenario_return=worst.return_on_standardized_capital_executable,
        quality_note=quality_note,
    )


# --------------------------------------------------------------------------
# T+1 profitable region vs. traditional expiration breakeven (Section 21).
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class T1ProfitableRegionDiagnostic:
    expiration_breakevens: tuple[Decimal, ...]
    t1_modeled_profitable_labels: tuple[str, ...]
    t1_modeled_unprofitable_labels: tuple[str, ...]
    iv_scenario_used_for_region: str
    note: str


def compute_expiration_breakevens(legs: tuple[V4T1LegInput, ...]) -> tuple[Decimal, ...] | None:
    """Reuses analytics/options/payoff.py::analyze directly -- the
    same exhaustive piecewise-linear breakeven finder V3's own
    strategy_candidates.py already relies on. The TRADITIONAL,
    expiration-payoff breakeven, never touched or replaced (Section
    21) -- kept explicitly separate from the T+1 modeled region below."""
    option_legs = []
    for leg in legs:
        price = leg.entry_executable_price
        if price is None:
            return None
        option_legs.append(
            OptionLeg(
                option_type=OptionType(leg.right),
                action=Action(leg.action),
                strike=leg.strike,
                premium=price,
                quantity=leg.quantity,
            )
        )
    return analyze(option_legs).breakevens


def compute_t1_profitable_region(
    results: tuple[T1ScenarioResult, ...],
    iv_label: str,
    legs: tuple[V4T1LegInput, ...],
) -> T1ProfitableRegionDiagnostic:
    """Section 21 -- a DIFFERENT diagnostic from expiration breakeven,
    displayed separately, never overwriting it. Held at one fixed IV
    scenario (``iv_label``) since the scenario grid is discrete, not a
    continuous function of underlying price alone -- reporting which
    of the 7 real underlying-move labels are modeled profitable/
    unprofitable at that IV level, not a single continuous price."""
    relevant = [r for r in results if r.iv_scenario_label == iv_label]
    profitable = tuple(
        r.underlying_move_label
        for r in relevant
        if r.realized_equivalent_pnl_executable is not None
        and r.realized_equivalent_pnl_executable > 0
    )
    unprofitable = tuple(
        r.underlying_move_label
        for r in relevant
        if r.realized_equivalent_pnl_executable is not None
        and r.realized_equivalent_pnl_executable <= 0
    )
    breakevens = compute_expiration_breakevens(legs) or ()
    return T1ProfitableRegionDiagnostic(
        expiration_breakevens=breakevens,
        t1_modeled_profitable_labels=profitable,
        t1_modeled_unprofitable_labels=unprofitable,
        iv_scenario_used_for_region=iv_label,
        note=(
            "EXPIRATION BREAKEVEN (traditional, analytics/options/payoff.py::analyze) and T+1 "
            "MODELED PROFITABLE REGION (this module's own scenario grid, held at one fixed IV "
            "scenario) are DIFFERENT concepts, displayed separately, never conflated (Section 21)."
        ),
    )
