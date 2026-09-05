"""How a settled position was actually priced -- kept explicit, because a
closing mark is not a fill.

The end-of-day fallback (v4.1.0) made it possible to settle a position whose
executable side was empty, which is what stops positions being stranded. It
also means "SETTLED" no longer implies "closed at a real executable price".
Reporting both under one realized-performance number would quietly overstate
how much of the forward record is executable evidence, so every settlement
carries one of these grades and the Track Record reports them separately.

Grades are ordered worst-wins: a multi-leg settlement is only EXECUTABLE
when EVERY leg was priced on its own required side. One leg on a closing
mark grades the whole settlement as a closing-mark settlement, because the
net exit value it produced is no longer something that could have been
transacted at that price.
"""

from collections.abc import Iterable
from dataclasses import dataclass, field

from models.v4_shadow import V4ShadowConfigSettlement
from services.v4_settlement_fallback import (
    PRICING_EXPIRATION_INTRINSIC_AT_CLOSE,
    PRICING_MARKET_CLOSE_FALLBACK,
)

GRADE_EXECUTABLE = "EXECUTABLE_BID_ASK"
GRADE_MARKET_CLOSE = "MARKET_CLOSE_FALLBACK"
GRADE_INTRINSIC = "EXPIRATION_INTRINSIC_AT_CLOSE"
GRADE_UNRESOLVED = "UNRESOLVED"

#: Worst grade wins when a settlement mixes pricing sources.
GRADE_SEVERITY = {
    GRADE_EXECUTABLE: 0,
    GRADE_MARKET_CLOSE: 1,
    GRADE_INTRINSIC: 2,
    GRADE_UNRESOLVED: 3,
}

#: The grades whose exit value could genuinely have been transacted.
EXECUTABLE_GRADES = frozenset({GRADE_EXECUTABLE})

GRADE_LABELS = {
    GRADE_EXECUTABLE: "Executable bid/ask",
    GRADE_MARKET_CLOSE: "End-of-day closing mark",
    GRADE_INTRINSIC: "Expiration intrinsic value",
    GRADE_UNRESOLVED: "Unresolved",
}


def settlement_grade(row: V4ShadowConfigSettlement) -> str:
    """The evidence grade of ONE settlement.

    A settlement written before the end-of-day fallback existed carries no
    ``pricing_method``; those could only ever be written when every required
    side was present, so they grade EXECUTABLE. Anything unsettled grades
    UNRESOLVED regardless of what it was priced with.
    """
    if row.status != "SETTLED":
        return GRADE_UNRESOLVED
    method = row.pricing_method or ""
    if PRICING_EXPIRATION_INTRINSIC_AT_CLOSE in method:
        return GRADE_INTRINSIC
    if PRICING_MARKET_CLOSE_FALLBACK in method:
        return GRADE_MARKET_CLOSE
    return GRADE_EXECUTABLE


@dataclass
class SettlementQualityBreakdown:
    """Counts and rates over a set of settlements of record."""

    total: int = 0
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def rates(self) -> dict[str, float]:
        if not self.total:
            return {grade: 0.0 for grade in GRADE_SEVERITY}
        return {
            grade: self.counts.get(grade, 0) / self.total for grade in GRADE_SEVERITY
        }

    @property
    def executable_rate(self) -> float:
        return self.rates[GRADE_EXECUTABLE]

    @property
    def fallback_rate(self) -> float:
        return self.rates[GRADE_MARKET_CLOSE]

    @property
    def intrinsic_rate(self) -> float:
        return self.rates[GRADE_INTRINSIC]

    @property
    def unresolved_rate(self) -> float:
        return self.rates[GRADE_UNRESOLVED]


def summarize_settlement_quality(
    rows: Iterable[V4ShadowConfigSettlement],
) -> SettlementQualityBreakdown:
    out = SettlementQualityBreakdown()
    for row in rows:
        grade = settlement_grade(row)
        out.counts[grade] = out.counts.get(grade, 0) + 1
        out.total += 1
    for grade in GRADE_SEVERITY:
        out.counts.setdefault(grade, 0)
    return out


def executable_only(
    rows: Iterable[V4ShadowConfigSettlement],
) -> list[V4ShadowConfigSettlement]:
    """The subset whose exit value was a real executable price on every leg.

    An analytics filter, never a deletion: the excluded settlements remain
    exactly as persisted and are still reported under All Outcomes.
    """
    return [row for row in rows if settlement_grade(row) in EXECUTABLE_GRADES]
