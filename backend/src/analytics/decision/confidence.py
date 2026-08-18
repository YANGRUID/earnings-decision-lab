"""Deterministic confidence scoring for the AI Options Decision Engine
(Phase 14.9). This is NOT the LLM's self-reported confidence -- an LLM
asking itself "how sure am I" produces an arbitrary, unfalsifiable number.
Instead, ``confidence_score`` is built entirely from real, already-computed
signals about how much evidence actually exists to support a directional
view: how many evidence sources returned real data, whether analyst
estimate revisions agree with the stated direction, whether this
company's own historical earnings moves have agreed with that direction,
how fresh the underlying snapshots are, and how complete the real
options-market pricing is. Every component is a plain, auditable
computation over real data -- never an LLM judgment, never a probability
of a correct trade (see analytics/options/strategy_ranking.py's own
disclaimer against exactly that).

None of this is, or claims to be, a probability that the stated direction
will turn out correct. It answers a narrower, honest question: "how much
real evidence backs this view, right now" -- see
docs/decision_methodology.md-equivalent explanation surfaced in the
frontend Decision View.
"""

from dataclasses import dataclass
from decimal import Decimal

from models.enums import DecisionDirection, RevisionDirection

# Component weights, summing to 100 -- see the module docstring for what
# each represents. Kept as named constants (not magic numbers scattered
# through the function body) so the methodology is easy to audit and to
# change deliberately in one place.
WEIGHT_EVIDENCE_COVERAGE = 25
WEIGHT_CONSENSUS_AGREEMENT = 20
WEIGHT_HISTORICAL_CONSISTENCY = 20
WEIGHT_DATA_FRESHNESS = 15
WEIGHT_OPTIONS_COMPLETENESS = 20

assert (
    WEIGHT_EVIDENCE_COVERAGE
    + WEIGHT_CONSENSUS_AGREEMENT
    + WEIGHT_HISTORICAL_CONSISTENCY
    + WEIGHT_DATA_FRESHNESS
    + WEIGHT_OPTIONS_COMPLETENESS
    == 100
)


@dataclass(frozen=True)
class ConfidenceComponents:
    evidence_coverage: int
    consensus_agreement: int
    historical_consistency: int
    data_freshness: int
    options_completeness: int

    @property
    def total(self) -> int:
        return (
            self.evidence_coverage
            + self.consensus_agreement
            + self.historical_consistency
            + self.data_freshness
            + self.options_completeness
        )

    def as_dict(self) -> dict[str, int]:
        return {
            "evidence_coverage": self.evidence_coverage,
            "consensus_agreement": self.consensus_agreement,
            "historical_consistency": self.historical_consistency,
            "data_freshness": self.data_freshness,
            "options_completeness": self.options_completeness,
        }


_DIRECTION_SIGN: dict[DecisionDirection, int] = {
    DecisionDirection.STRONG_BULLISH: 1,
    DecisionDirection.BULLISH: 1,
    DecisionDirection.NEUTRAL: 0,
    DecisionDirection.BEARISH: -1,
    DecisionDirection.STRONG_BEARISH: -1,
}


def _evidence_coverage_score(evidence_tool_success_count: int, evidence_tool_total: int) -> int:
    if evidence_tool_total <= 0:
        return 0
    fraction = Decimal(evidence_tool_success_count) / Decimal(evidence_tool_total)
    return round(fraction * WEIGHT_EVIDENCE_COVERAGE)


def _consensus_agreement_score(
    direction: DecisionDirection, revision_direction: RevisionDirection | None
) -> int:
    view_sign = _DIRECTION_SIGN[direction]
    if revision_direction is None or revision_direction == RevisionDirection.UNKNOWN:
        return round(WEIGHT_CONSENSUS_AGREEMENT * Decimal("0.25"))  # no data -- low, not zero
    if view_sign == 0:
        # A neutral view is best supported by flat/unclear revisions.
        if revision_direction == RevisionDirection.FLAT:
            return WEIGHT_CONSENSUS_AGREEMENT
        return round(WEIGHT_CONSENSUS_AGREEMENT * Decimal("0.5"))
    revision_sign = {"up": 1, "down": -1, "flat": 0}[revision_direction.value]
    if revision_sign == view_sign:
        return WEIGHT_CONSENSUS_AGREEMENT
    if revision_sign == 0:
        return round(WEIGHT_CONSENSUS_AGREEMENT * Decimal("0.5"))
    return 0  # revisions move opposite the stated view


def _historical_consistency_score(
    direction: DecisionDirection, historical_move_pcts: list[Decimal]
) -> int:
    if not historical_move_pcts:
        return round(WEIGHT_HISTORICAL_CONSISTENCY * Decimal("0.25"))  # no data -- low, not zero
    view_sign = _DIRECTION_SIGN[direction]
    if view_sign == 0:
        # A neutral view is supported by small historical moves, not by
        # directional agreement (there's no direction to agree with).
        small = [m for m in historical_move_pcts if abs(m) <= Decimal("0.03")]
        fraction = Decimal(len(small)) / Decimal(len(historical_move_pcts))
    else:
        matching = [m for m in historical_move_pcts if (m > 0) == (view_sign > 0)]
        fraction = Decimal(len(matching)) / Decimal(len(historical_move_pcts))
    return round(fraction * WEIGHT_HISTORICAL_CONSISTENCY)


def _data_freshness_score(snapshot_age_minutes: int | None) -> int:
    if snapshot_age_minutes is None:
        return 0
    if snapshot_age_minutes <= 60:
        return WEIGHT_DATA_FRESHNESS
    if snapshot_age_minutes <= 24 * 60:
        return round(WEIGHT_DATA_FRESHNESS * Decimal("0.66"))
    if snapshot_age_minutes <= 7 * 24 * 60:
        return round(WEIGHT_DATA_FRESHNESS * Decimal("0.33"))
    return 0


def _options_completeness_score(chain_exists: bool, implied_move_available: bool) -> int:
    if implied_move_available:
        return WEIGHT_OPTIONS_COMPLETENESS
    if chain_exists:
        return round(WEIGHT_OPTIONS_COMPLETENESS * Decimal("0.5"))
    return 0


def compute_confidence(
    *,
    direction: DecisionDirection,
    evidence_tool_success_count: int,
    evidence_tool_total: int,
    revision_direction: RevisionDirection | None,
    historical_move_pcts: list[Decimal],
    snapshot_age_minutes: int | None,
    chain_exists: bool,
    implied_move_available: bool,
) -> ConfidenceComponents:
    """Every argument here is a real, already-computed signal -- none of
    them are asked of the LLM. See the module docstring for what each
    component measures and why it's bounded the way it is."""
    return ConfidenceComponents(
        evidence_coverage=_evidence_coverage_score(
            evidence_tool_success_count, evidence_tool_total
        ),
        consensus_agreement=_consensus_agreement_score(direction, revision_direction),
        historical_consistency=_historical_consistency_score(direction, historical_move_pcts),
        data_freshness=_data_freshness_score(snapshot_age_minutes),
        options_completeness=_options_completeness_score(chain_exists, implied_move_available),
    )
