"""Options Decision Engine V4.1 -- Candidate Evaluation Feature Contract
(2026-08-31).

The typed structure a future V4.2/V4.4 candidate evaluator will
eventually receive. Defined now, ahead of any evaluator that consumes it,
so every later V4 stage is designed against one explicit, shared input
shape instead of an ad hoc argument list that grows field-by-field.

NOTHING BUILDS ONE OF THESE YET. There is no real constructor call
anywhere in the official pipeline -- this is a pure data contract,
exercised only by this module's own tests. Every field that a real
DecisionSnapshot/EntryCaptureAttempt/OptionQuote cannot always supply is
Optional; nothing here is ever fabricated to fill a gap (the project's
own established convention -- see e.g. HPQ's real, genuinely-None
implied_move_pct and earnings_date, both correctly carried through rather
than guessed).

Holding period is FIRST-CLASS here on purpose (Section 8): every future
V4 scoring/ranking API must be designed with the real ~1-day forced exit
in view from the start, never bolted on after the fact the way V3's own
DTE/theta reasoning never once accounted for it (forensic audit Part I
Section 3/7).
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from analytics.decision.v4_strategy_semantics import StrategySemantics
from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.enums import DecisionDirection, DecisionVolatilityView


@dataclass(frozen=True)
class AiViewFeatures:
    direction: DecisionDirection
    volatility_view: DecisionVolatilityView
    evidence_confidence: int | None  # deterministic_confidence_score -- never LLM self-reported


@dataclass(frozen=True)
class MarketFeatures:
    underlying_price: Decimal
    expected_move_pct: Decimal | None
    expected_move_dollars: Decimal | None
    atm_implied_volatility: Decimal | None
    market_data_quality: str | None


@dataclass(frozen=True)
class EventFeatures:
    earnings_date: date | None
    earnings_timing: str | None


@dataclass(frozen=True)
class HoldingPeriodFeatures:
    """First-class, not an afterthought -- see this module's own
    docstring. ``dte_at_entry``/``dte_at_exit`` are both real, computable
    facts about the SAME selected contract; V4.1 only carries them, it
    does not yet use them to price or forecast anything (that is V4.4)."""

    entry_timestamp: datetime
    expected_exit_timestamp: datetime
    holding_period_seconds: int
    holding_trading_sessions: int
    dte_at_entry: int
    dte_at_exit: int


@dataclass(frozen=True)
class ContractFeatures:
    expiration: date
    dte_at_entry: int


@dataclass(frozen=True)
class StrategyFeatures:
    category: StrategyCategory
    semantics: StrategySemantics
    candidate: StrategyCandidate
    strikes: tuple[Decimal, ...]
    widths: tuple[Decimal, ...]


@dataclass(frozen=True)
class ExecutionQualityFeatures:
    bid_ask_contract_count: int
    contract_count: int
    quote_coverage: float | None
    volume_coverage: float | None
    open_interest_coverage: float | None
    median_spread_pct: Decimal | None


@dataclass(frozen=True)
class HistoricalFeatures:
    historical_move_pcts: tuple[Decimal, ...]
    sample_size: int


@dataclass(frozen=True)
class V4CandidateContext:
    """Everything a future V4 candidate evaluator would need, grouped
    exactly as this task's own spec names them. Every sub-group is its
    own frozen dataclass so a future evaluator can depend on (and test)
    one slice at a time."""

    ai_view: AiViewFeatures
    market: MarketFeatures
    event: EventFeatures
    holding_period: HoldingPeriodFeatures
    contract: ContractFeatures
    strategy: StrategyFeatures
    execution_quality: ExecutionQualityFeatures
    historical: HistoricalFeatures
