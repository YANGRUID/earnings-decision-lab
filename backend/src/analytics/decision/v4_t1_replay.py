"""Options Decision Engine V4.4A -- Real V3 Decision T+1 Scenario
Replay (2026-09-03).

Replays real, already-frozen V3 DecisionSnapshot rows through V4.4A's
scenario valuation engine, using ONLY decision-time information (real
captured chain quotes, including real per-strike IV where it survives)
-- never today's IV, never a live re-fetch, never a backfilled value
(Section 34's own explicit anti-lookahead rule).

CANNOT_REPLAY_HONESTLY, NOT A BACKFILL (Section 34). V4.4A's whole
point is to demonstrate real Black-Scholes repricing -- that requires
a real per-leg entry IV for EVERY leg V3 actually selected. Real
market data has real gaps (this project's own earlier audit found
several tickers with sparse per-strike IV coverage, e.g. only 1 of 22
real CRM chain rows carries a real IV). When ANY selected leg lacks a
real IV, this module marks the WHOLE candidate CANNOT_REPLAY_HONESTLY
rather than silently degrading just that leg -- a replay is supposed
to demonstrate the methodology working end to end, not paper over a
real gap with a partial result.

POST-HOC ENVELOPE SANITY CHECK ONLY (Section 36). ``envelope_check``
compares a real REALIZED T+1 outcome (already-settled, from
ExitSnapshot) against the modeled scenario envelope purely for
validation -- it is never fed back into the engine, never used to
retune any scenario/friction/crush parameter (Section 35). Called only
AFTER the engine's own tests already pass; this module does not run
automatically as part of the engine's own construction.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_t1_pricing import (
    T1CandidateDistributionSummary,
    T1ScenarioResult,
    evaluate_candidate_t1_scenarios,
    summarize_candidate_distribution,
)
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from analytics.options.strategy_candidates import StrategyCategory
from providers.types import OptionQuote

T1_REPLAY_VERSION = "t1_replay_v1"

CANNOT_REPLAY_HONESTLY = "CANNOT_REPLAY_HONESTLY"

# (action, option_type, strike) -- the real leg V3 actually selected.
V3LegSummary = tuple[str, str, Decimal]


@dataclass(frozen=True)
class V3T1ReplayInput:
    """Decision-time-only fields -- deliberately excludes every
    settlement/outcome field that exists on the real row."""

    ticker: str
    strategy_type: str | None
    underlying_price: Decimal | None
    entry_timestamp: datetime | None
    expected_exit_timestamp: datetime | None
    expiration: date | None
    v3_legs: tuple[V3LegSummary, ...]
    chain_quotes: tuple[OptionQuote, ...] | None
    expected_move_context: ExpectedMoveContext | None


@dataclass(frozen=True)
class V3T1ReplayResult:
    ticker: str
    strategy_type: str | None
    replayable: bool
    skip_reason: str | None
    scenario_results: tuple[T1ScenarioResult, ...] | None
    distribution_summary: T1CandidateDistributionSummary | None


def _find_quote(quotes: tuple[OptionQuote, ...], strike: Decimal, right: str) -> OptionQuote | None:
    return next((q for q in quotes if q.strike == strike and q.option_type == right), None)


def replay_v3_t1_scenario(decision: V3T1ReplayInput) -> V3T1ReplayResult:
    if not decision.strategy_type:
        return V3T1ReplayResult(
            ticker=decision.ticker,
            strategy_type=None,
            replayable=False,
            skip_reason="NO_ACTION -- no strategy was selected, nothing to replay.",
            scenario_results=None,
            distribution_summary=None,
        )

    try:
        category = StrategyCategory(decision.strategy_type)
    except ValueError:
        return V3T1ReplayResult(
            ticker=decision.ticker,
            strategy_type=decision.strategy_type,
            replayable=False,
            skip_reason=f"Unrecognized strategy_type {decision.strategy_type!r}.",
            scenario_results=None,
            distribution_summary=None,
        )

    if (
        decision.underlying_price is None
        or decision.entry_timestamp is None
        or decision.expected_exit_timestamp is None
        or decision.expiration is None
        or decision.expected_move_context is None
    ):
        return V3T1ReplayResult(
            ticker=decision.ticker,
            strategy_type=decision.strategy_type,
            replayable=False,
            skip_reason=(
                f"{CANNOT_REPLAY_HONESTLY} -- no real decision-time underlying price/timestamp/"
                "expiration/expected-move context is on record for this decision."
            ),
            scenario_results=None,
            distribution_summary=None,
        )

    if not decision.chain_quotes:
        return V3T1ReplayResult(
            ticker=decision.ticker,
            strategy_type=decision.strategy_type,
            replayable=False,
            skip_reason=f"{CANNOT_REPLAY_HONESTLY} -- no real captured options chain survives.",
            scenario_results=None,
            distribution_summary=None,
        )

    legs: list[V4T1LegInput] = []
    for index, (action, option_type, strike) in enumerate(decision.v3_legs):
        quote = _find_quote(decision.chain_quotes, strike, option_type)
        if quote is None:
            return V3T1ReplayResult(
                ticker=decision.ticker,
                strategy_type=decision.strategy_type,
                replayable=False,
                skip_reason=(
                    f"{CANNOT_REPLAY_HONESTLY} -- no real captured quote for the "
                    f"({option_type}, {strike}) leg V3 actually selected."
                ),
                scenario_results=None,
                distribution_summary=None,
            )
        if quote.implied_volatility is None:
            return V3T1ReplayResult(
                ticker=decision.ticker,
                strategy_type=decision.strategy_type,
                replayable=False,
                skip_reason=(
                    f"{CANNOT_REPLAY_HONESTLY} -- no real entry IV survives for the "
                    f"({option_type}, {strike}) leg V3 actually selected; never backfilled."
                ),
                scenario_results=None,
                distribution_summary=None,
            )
        legs.append(
            V4T1LegInput(
                leg_index=index,
                action=action,  # type: ignore[arg-type]
                right=option_type,  # type: ignore[arg-type]
                strike=strike,
                quantity=2
                if category == StrategyCategory.LONG_CALL_BUTTERFLY and action == "sell"
                else 1,
                multiplier=Decimal("100"),
                entry_bid=quote.bid,
                entry_ask=quote.ask,
                entry_last=quote.last_price,
                entry_iv=quote.implied_volatility,
                entry_delta=quote.delta,
                entry_gamma=quote.gamma,
                entry_theta=quote.theta,
                entry_vega=quote.vega,
                market_data_quality=quote.market_data_quality,
                external_contract_id=quote.external_contract_id,
            )
        )

    context = V4T1ValuationContext(
        ticker=decision.ticker,
        underlying_price=decision.underlying_price,
        observed_at=decision.entry_timestamp,
        entry_timestamp=decision.entry_timestamp,
        expected_exit_timestamp=decision.expected_exit_timestamp,
        strategy=category,
        expiration=decision.expiration,
        legs=tuple(legs),
        expected_move_context=decision.expected_move_context,
    )

    results = evaluate_candidate_t1_scenarios(context, variant_id="v3_replay")
    if results is None:
        return V3T1ReplayResult(
            ticker=decision.ticker,
            strategy_type=decision.strategy_type,
            replayable=False,
            skip_reason=(
                f"{CANNOT_REPLAY_HONESTLY} -- neither implied move nor historical median move "
                "survives to build the underlying-move scenario grid."
            ),
            scenario_results=None,
            distribution_summary=None,
        )

    return V3T1ReplayResult(
        ticker=decision.ticker,
        strategy_type=decision.strategy_type,
        replayable=True,
        skip_reason=None,
        scenario_results=results,
        distribution_summary=summarize_candidate_distribution(results),
    )


def replay_many_t1_scenarios(decisions: list[V3T1ReplayInput]) -> list[V3T1ReplayResult]:
    return [replay_v3_t1_scenario(d) for d in decisions]


# --------------------------------------------------------------------------
# Post-hoc envelope sanity check (Section 36) -- validation only.
# --------------------------------------------------------------------------

EnvelopeCheckStatus = str  # "INSIDE_ENVELOPE" | "OUTSIDE_ENVELOPE" | "CANNOT_EVALUATE"

INSIDE_ENVELOPE = "INSIDE_ENVELOPE"
OUTSIDE_ENVELOPE = "OUTSIDE_ENVELOPE"
CANNOT_EVALUATE = "CANNOT_EVALUATE"


@dataclass(frozen=True)
class EnvelopeCheckResult:
    ticker: str
    status: EnvelopeCheckStatus
    realized_return_on_standardized_capital: Decimal | None
    modeled_min_return: Decimal | None
    modeled_max_return: Decimal | None
    note: str


def check_realized_outcome_inside_envelope(
    ticker: str,
    realized_pnl: Decimal | None,
    distribution_summary: T1CandidateDistributionSummary | None,
    standardized_capital: Decimal,
) -> EnvelopeCheckResult:
    """Pure comparison, called only AFTER the engine is frozen by its
    own tests -- never used to retune any V4.4A parameter (Section 35/
    36's own explicit rule: "Do not retune the engine afterward during
    this task"). ``realized_pnl`` should be a real, already-settled
    dollar P&L from ExitSnapshot/SettlementCaptureAttempt -- read here
    only for comparison, never written back anywhere."""
    if (
        realized_pnl is None
        or distribution_summary is None
        or distribution_summary.min_return is None
    ):
        return EnvelopeCheckResult(
            ticker=ticker,
            status=CANNOT_EVALUATE,
            realized_return_on_standardized_capital=None,
            modeled_min_return=None,
            modeled_max_return=None,
            note="Missing real realized P&L or modeled distribution -- cannot evaluate.",
        )
    realized_return = realized_pnl / standardized_capital
    assert distribution_summary.max_return is not None
    inside = distribution_summary.min_return <= realized_return <= distribution_summary.max_return
    return EnvelopeCheckResult(
        ticker=ticker,
        status=INSIDE_ENVELOPE if inside else OUTSIDE_ENVELOPE,
        realized_return_on_standardized_capital=realized_return,
        modeled_min_return=distribution_summary.min_return,
        modeled_max_return=distribution_summary.max_return,
        note=(
            f"Real realized return {realized_return:.4f} vs. modeled scenario envelope "
            f"[{distribution_summary.min_return:.4f}, {distribution_summary.max_return:.4f}]."
        ),
    )
