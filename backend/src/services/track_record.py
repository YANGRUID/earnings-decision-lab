"""Honest reliability/track-record metrics for the AI Options Decision
journal (Phase 14.9 Part H). Every metric here is computed only over
SETTLED decisions (real post-earnings outcome data on record -- see
services/decision_settlement.py) and always carries its own real sample
size. A metric with no real settled decisions behind it is None, not 0 --
this module never presents "no data yet" as "0% accuracy."

Three distinct, precisely-defined metrics that must never be confused
(Phase 14.9 Part H section 26):
- Directional Accuracy: did the stock move in the predicted direction.
- Breakeven Success: did the underlying finish beyond the recommended
  strategy's breakeven.
- Strategy Win Rate: did the actual recorded options position produce
  positive P&L -- ONLY computable when real point-in-time option
  entry/exit prices exist, which this project does not yet capture (see
  services/decision_settlement.py) -- so this metric is always
  "not available" today, honestly, rather than silently substituted with
  one of the other two.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from sqlalchemy.orm import Session

from models.ai_decision_version import AIDecisionVersion
from models.company import Company
from models.enums import DecisionDirection, DecisionStatus

Window = Literal["last_10", "all_time"]

_DIRECTIONAL_SIGN: dict[DecisionDirection, int] = {
    DecisionDirection.STRONG_BULLISH: 1,
    DecisionDirection.BULLISH: 1,
    DecisionDirection.NEUTRAL: 0,
    DecisionDirection.BEARISH: -1,
    DecisionDirection.STRONG_BEARISH: -1,
}

HIGH_CONFIDENCE_THRESHOLD = 70


@dataclass(frozen=True)
class Rate:
    """A fraction with its real sample size attached -- never displayed
    without one (Phase 14.9 Part H section 27: "Never show 67% without
    sample size")."""

    correct: int
    total: int

    @property
    def pct(self) -> Decimal | None:
        if self.total == 0:
            return None
        return Decimal(self.correct) / Decimal(self.total)


@dataclass(frozen=True)
class ConfidenceBucket:
    label: str
    lower: int
    upper: int
    rate: Rate


@dataclass(frozen=True)
class TrackRecordSummary:
    window: Window
    evaluated_count: int
    directional_accuracy: Rate
    bullish_accuracy: Rate
    bearish_accuracy: Rate
    average_confidence: Decimal | None
    high_confidence_accuracy: Rate
    volatility_view_accuracy: Rate
    breakeven_success: Rate
    strategy_win_rate_available: bool  # always False today -- see module docstring
    confidence_calibration: list[ConfidenceBucket]


def _fetch_settled(db: Session, *, ticker: str | None, window: Window) -> list[AIDecisionVersion]:
    query = (
        db.query(AIDecisionVersion)
        .join(Company, AIDecisionVersion.company_id == Company.id)
        .filter(AIDecisionVersion.status == DecisionStatus.SETTLED)
    )
    if ticker is not None:
        query = query.filter(Company.ticker == ticker)
    query = query.order_by(AIDecisionVersion.settled_at.desc(), AIDecisionVersion.id.desc())
    if window == "last_10":
        query = query.limit(10)
    return query.all()


def _directional_rate(decisions: list[AIDecisionVersion], sign_filter: int | None) -> Rate:
    def matches(d: AIDecisionVersion) -> bool:
        if d.direction_correct is None:
            return False
        if sign_filter is None:
            return True
        return _DIRECTIONAL_SIGN[DecisionDirection(d.direction)] == sign_filter

    relevant = [d for d in decisions if matches(d)]
    correct = sum(1 for d in relevant if d.direction_correct)
    return Rate(correct=correct, total=len(relevant))


def _volatility_view_rate(decisions: list[AIDecisionVersion]) -> Rate:
    """long_vol is "correct" when the actual move exceeded the implied
    move; short_vol is "correct" when it did not. neutral_vol is excluded
    -- the same "no direction was called, nothing to grade" reasoning as
    a neutral directional view."""
    relevant = [d for d in decisions if d.actual_move_exceeded_implied is not None]
    correct = 0
    total = 0
    for d in relevant:
        if d.volatility_view == "long_vol":
            total += 1
            correct += 1 if d.actual_move_exceeded_implied else 0
        elif d.volatility_view == "short_vol":
            total += 1
            correct += 1 if not d.actual_move_exceeded_implied else 0
    return Rate(correct=correct, total=total)


def _breakeven_rate(decisions: list[AIDecisionVersion]) -> Rate:
    relevant = [d for d in decisions if d.breakeven_met is not None]
    correct = sum(1 for d in relevant if d.breakeven_met)
    return Rate(correct=correct, total=len(relevant))


def _confidence_calibration(decisions: list[AIDecisionVersion]) -> list[ConfidenceBucket]:
    buckets = [
        ("0-59", 0, 59),
        ("60-69", 60, 69),
        ("70-79", 70, 79),
        ("80-89", 80, 89),
        ("90-100", 90, 100),
    ]
    result = []
    for label, lower, upper in buckets:
        in_bucket = [
            d
            for d in decisions
            if lower <= d.confidence_score <= upper and d.direction_correct is not None
        ]
        correct = sum(1 for d in in_bucket if d.direction_correct)
        rate = Rate(correct=correct, total=len(in_bucket))
        result.append(ConfidenceBucket(label=label, lower=lower, upper=upper, rate=rate))
    return result


def compute_track_record(
    db: Session, *, ticker: str | None = None, window: Window = "all_time"
) -> TrackRecordSummary:
    decisions = _fetch_settled(db, ticker=ticker, window=window)

    avg_confidence = (
        Decimal(sum(d.confidence_score for d in decisions)) / Decimal(len(decisions))
        if decisions
        else None
    )
    high_confidence = [d for d in decisions if d.confidence_score >= HIGH_CONFIDENCE_THRESHOLD]

    return TrackRecordSummary(
        window=window,
        evaluated_count=len(decisions),
        directional_accuracy=_directional_rate(decisions, sign_filter=None),
        bullish_accuracy=_directional_rate(decisions, sign_filter=1),
        bearish_accuracy=_directional_rate(decisions, sign_filter=-1),
        average_confidence=avg_confidence,
        high_confidence_accuracy=_directional_rate(high_confidence, sign_filter=None),
        volatility_view_accuracy=_volatility_view_rate(decisions),
        breakeven_success=_breakeven_rate(decisions),
        strategy_win_rate_available=False,
        confidence_calibration=_confidence_calibration(decisions),
    )
