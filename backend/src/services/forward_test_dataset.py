"""Phase 4 forward-test evaluation dataset (2026-08-26), Section 32-33 --
a canonical, READ-ONLY view over the existing official Phase 4 evidence
tables (DecisionSnapshot, EntryCaptureAttempt/EntrySnapshot,
SettlementCaptureAttempt/ExitSnapshot -- never the older ai_decision_
version scaffold). Built for future evaluation/modeling work; does NOT
itself train, fit, or calibrate anything (see Section 34 -- explicitly
deferred, insufficient real settled sample size today).

Every field here is either read directly off an existing, already-
frozen/immutable row, or a pure, deterministic derivation over such rows
-- never invented, never estimated, never backfilled. ``directional_
correctness`` is intentionally ``None`` for a NEUTRAL decision (no
directional bet exists to grade). ``breakeven_held`` is the strict
payoff-at-real-underlying-price check using this decision's own frozen
legs (analytics/options/payoff.py -- same math the engine itself used to
build the position), a genuinely distinct signal from ``is_win``
(SettlementCaptureAttempt's own realized-P&L outcome, which reflects
real captured bid/ask entry/exit prices and therefore real transaction
friction breakeven_held does not).
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.market_data_policy import derive_capture_quality_label
from analytics.options.payoff import Action, OptionLeg
from models.decision_snapshot import DecisionSnapshot
from models.entry_capture_attempt import EntryCaptureAttempt
from models.enums import CaptureStatus, OptionType
from models.settlement_capture_attempt import SettlementCaptureAttempt

# Bounded, matches this project's other cross-decision list endpoints
# (e.g. services/decision_history.py) -- a personal research tool with a
# small real decision count, never an unbounded batch query.
MAX_DATASET_ROWS = 500


@dataclass(frozen=True)
class EntryLegRow:
    leg_index: int
    option_type: str | None
    strike: Decimal | None
    action: str | None
    bid: Decimal | None
    ask: Decimal | None
    benchmark_entry_price: Decimal | None
    pricing_assumption: str | None
    delta: Decimal | None
    gamma: Decimal | None
    theta: Decimal | None
    vega: Decimal | None
    market_data_quality: str | None


@dataclass(frozen=True)
class ForwardTestDatasetRow:
    # --- Identity -----------------------------------------------------
    decision_snapshot_id: int
    ticker: str
    generated_at: datetime

    # --- PRE-EVENT ------------------------------------------------------
    direction: str
    volatility_view: str | None
    effective_risk_profile: str | None
    strategy_type: str | None
    selected_expiration: date | None
    dte_at_generation: int | None
    legs: list | None
    implied_volatility: Decimal | None
    volatility_regime: str | None
    score_breakdown: dict | None  # the 9 real strategy-scoring components
    strategy_score: int | None
    deterministic_confidence_score: int | None
    historical_compatibility: dict | None
    historical_sample_size: int | None
    confidence_interval: dict | None

    # --- ENTRY ----------------------------------------------------------
    entry_status: str | None
    entry_underlying_price: Decimal | None
    entry_net_price_per_share: Decimal | None
    entry_capital_at_risk: Decimal | None
    entry_legs: list[EntryLegRow] | None
    entry_market_data_quality_label: str | None

    # --- POST-EVENT (once settled) --------------------------------------
    settlement_status: str | None
    exit_underlying_price: Decimal | None
    realized_pnl: Decimal | None
    return_pct: Decimal | None
    r_multiple: Decimal | None
    is_win: bool | None
    settlement_market_data_quality_label: str | None

    # --- Derived ----------------------------------------------------------
    underlying_move_pct: Decimal | None
    directional_correctness: bool | None
    breakeven_held: bool | None


_BULLISH_DIRECTIONS = {"bullish", "strong_bullish"}
_BEARISH_DIRECTIONS = {"bearish", "strong_bearish"}


def _directional_correctness(direction: str, move_pct: Decimal | None) -> bool | None:
    """None for NEUTRAL (no directional bet to grade) or when the real
    move can't be computed -- never a guessed 50/50 coin flip."""
    if move_pct is None:
        return None
    if direction in _BULLISH_DIRECTIONS:
        return move_pct > 0
    if direction in _BEARISH_DIRECTIONS:
        return move_pct < 0
    return None


def _breakeven_held(legs_json: list | None, exit_underlying_price: Decimal | None) -> bool | None:
    """Strict payoff-at-real-underlying-price check using this decision's
    own frozen legs -- distinct from is_win (which reflects real captured
    entry/exit prices, including transaction friction is_win does not
    isolate from). None when legs or the real exit price aren't known."""
    if not legs_json or exit_underlying_price is None:
        return None
    try:
        option_legs = [
            OptionLeg(
                option_type=OptionType(leg["option_type"]),
                action=Action(leg["action"]),
                strike=Decimal(leg["strike"]),
                premium=Decimal(leg["premium"]),
                quantity=int(leg["quantity"]),
            )
            for leg in legs_json
        ]
    except (KeyError, ValueError, TypeError):
        return None
    total_payoff = sum((leg.payoff(exit_underlying_price) for leg in option_legs), Decimal(0))
    return total_payoff > 0


def _dte(selected_expiration: date | None, generated_at: datetime) -> int | None:
    if selected_expiration is None:
        return None
    return (selected_expiration - generated_at.date()).days


def build_forward_test_dataset_row(
    decision: DecisionSnapshot,
    entry_attempt: EntryCaptureAttempt | None,
    settlement_attempt: SettlementCaptureAttempt | None,
) -> ForwardTestDatasetRow:
    entry_legs = None
    entry_quality_values: list[str | None] = []
    if entry_attempt is not None:
        entry_legs = [
            EntryLegRow(
                leg_index=leg.leg_index,
                option_type=leg.option_type.value if leg.option_type else None,
                strike=leg.strike,
                action=leg.action.value if leg.action else None,
                bid=leg.bid,
                ask=leg.ask,
                benchmark_entry_price=leg.benchmark_entry_price,
                pricing_assumption=leg.pricing_assumption,
                delta=leg.delta,
                gamma=leg.gamma,
                theta=leg.theta,
                vega=leg.vega,
                market_data_quality=(
                    leg.market_data_quality.value if leg.market_data_quality else None
                ),
            )
            for leg in entry_attempt.legs
        ]
        entry_quality_values = [leg.market_data_quality for leg in entry_legs]

    settlement_quality_values: list[str | None] = []
    if settlement_attempt is not None:
        settlement_quality_values = [
            leg.market_data_quality.value if leg.market_data_quality else None
            for leg in settlement_attempt.legs
        ]

    exit_underlying_price = settlement_attempt.underlying_price if settlement_attempt else None
    move_pct = None
    if (
        entry_attempt is not None
        and entry_attempt.underlying_price is not None
        and exit_underlying_price is not None
        and entry_attempt.underlying_price != 0
    ):
        entry_price = entry_attempt.underlying_price
        move_pct = (exit_underlying_price - entry_price) / entry_price

    return ForwardTestDatasetRow(
        decision_snapshot_id=decision.id,
        ticker=decision.ticker,
        generated_at=decision.generated_at,
        direction=decision.strategy_direction.value,
        volatility_view=decision.volatility_view.value if decision.volatility_view else None,
        effective_risk_profile=(
            decision.effective_risk_profile.value if decision.effective_risk_profile else None
        ),
        strategy_type=decision.strategy_type,
        selected_expiration=decision.selected_expiration,
        dte_at_generation=_dte(decision.selected_expiration, decision.generated_at),
        legs=decision.legs,
        implied_volatility=decision.implied_volatility,
        volatility_regime=decision.volatility_regime,
        score_breakdown=decision.score_breakdown,
        strategy_score=decision.strategy_score,
        deterministic_confidence_score=decision.deterministic_confidence_score,
        historical_compatibility=decision.historical_compatibility,
        historical_sample_size=decision.historical_sample_size,
        confidence_interval=decision.confidence_interval,
        entry_status=entry_attempt.status.value if entry_attempt else None,
        entry_underlying_price=entry_attempt.underlying_price if entry_attempt else None,
        entry_net_price_per_share=(
            entry_attempt.net_entry_price_per_share if entry_attempt else None
        ),
        entry_capital_at_risk=entry_attempt.initial_max_risk if entry_attempt else None,
        entry_legs=entry_legs,
        entry_market_data_quality_label=(
            derive_capture_quality_label(entry_quality_values) if entry_attempt else None
        ),
        settlement_status=settlement_attempt.status.value if settlement_attempt else None,
        exit_underlying_price=exit_underlying_price,
        realized_pnl=settlement_attempt.realized_pnl if settlement_attempt else None,
        return_pct=settlement_attempt.return_pct if settlement_attempt else None,
        r_multiple=settlement_attempt.r_multiple if settlement_attempt else None,
        is_win=settlement_attempt.is_win if settlement_attempt else None,
        settlement_market_data_quality_label=(
            derive_capture_quality_label(settlement_quality_values) if settlement_attempt else None
        ),
        underlying_move_pct=move_pct,
        directional_correctness=_directional_correctness(
            decision.strategy_direction.value, move_pct
        ),
        breakeven_held=_breakeven_held(decision.legs, exit_underlying_price),
    )


def list_forward_test_dataset(
    db: Session, *, limit: int = MAX_DATASET_ROWS
) -> list[ForwardTestDatasetRow]:
    """Every real DecisionSnapshot, newest first, joined with its most
    recent real CAPTURED entry/settlement attempt when one exists (never
    a FAILED one -- a failed attempt has no real fill to expose here).
    """
    decisions = (
        db.query(DecisionSnapshot).order_by(DecisionSnapshot.generated_at.desc()).limit(limit).all()
    )
    rows = []
    for decision in decisions:
        entry_attempt = (
            db.query(EntryCaptureAttempt)
            .filter_by(decision_snapshot_id=decision.id, status=CaptureStatus.CAPTURED)
            .one_or_none()
        )
        settlement_attempt = (
            db.query(SettlementCaptureAttempt)
            .filter_by(decision_snapshot_id=decision.id, status=CaptureStatus.CAPTURED)
            .one_or_none()
        )
        rows.append(build_forward_test_dataset_row(decision, entry_attempt, settlement_attempt))
    return rows
