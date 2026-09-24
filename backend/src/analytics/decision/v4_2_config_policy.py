"""V4.2 PHASE 2 -- six independent configuration decisions over one universe.

THE DEFECT THIS REPLACES
------------------------
Phase 1 also has a per-configuration layer, and on 90 real production rows it
never once changed an outcome. Measured: every one of the 15 forward events
produced six configuration results whose text was identical except for the
configuration's own name, and CAPITAL_INCOMPATIBLE and RISK_CAP_EXCEEDED each
bound exactly zero times.

That is structural, not bad luck. Phase 1's per-configuration check asks only
two questions -- is the entry cash above this configuration's capital base, and
is the max loss above its risk cap -- and it is never given the max loss, so
only the first can fire. It cannot fire either, because the candidates it reads
are V4.1's RANKABLE rows, and V4.1 has already deleted everything whose entry
cash exceeds the $2,000 standardized capital (30 candidates across 8 events,
one of them a $29,750 structure, and 13 of AZO's 15). So the candidates that
could have separated $2K from $10K are gone before the configuration layer
sees them, and the six configurations are arithmetically forced to agree.

WHAT THIS DOES INSTEAD
----------------------
Each configuration applies the risk-profile truth this project already owns --
the family permission from ``is_category_allowed_for_profile``, the liquidity
floor from ``MIN_BID_ASK_COVERAGE``, the risk cap from
``DEFAULT_MAX_RISK_UTILIZATION_PCT`` -- to the SHARED valued universe, then
ranks what survives with the released V4.2 ranking order. No percentage is
invented here and no threshold is retuned: the absolute economic gate is
imported from Phase 1 unchanged and is deliberately identical across the six,
because an economically bad trade is bad at every size.

STAGE ORDER, AND WHY IT IS THIS ONE
-----------------------------------
Every rejected candidate is counted exactly once, at the FIRST stage that
refuses it, so the per-configuration counts sum to the universe. The
configuration-specific stages run BEFORE the shared economic gate on purpose:
if the absolute gate ran first, Conservative's own family and liquidity
refusals would be hidden behind an economics summary identical to
Aggressive's, which is precisely the indistinguishable diagnostics Phase 1
produced. Ordering the configuration's own rules first is what makes "why did
THIS configuration decline?" answerable.

    data honesty -> family -> liquidity -> economics -> move edge
                 -> capital -> risk cap -> rank

SIZING IS SEPARATE FROM SELECTION
---------------------------------
Quantity is computed after a candidate is chosen, never as a filter. A
structure that is too expensive for one configuration is excluded from THAT
configuration's set and the configuration goes on ranking the rest; it does
not collapse the event to NO_ACTION. NO_ACTION is returned only when a
configuration's own eligible set is genuinely empty.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from analytics.decision.risk_profile import (
    MIN_BID_ASK_COVERAGE,
    is_category_allowed_for_profile,
    meets_liquidity_gate,
)
from analytics.decision.v4_2_phase2_methodology import CONFIG_RANKING_VERSION
from analytics.decision.v4_2_viability import (
    DEFAULT_POLICY,
    CandidateEconomics,
    MoveEvidence,
    ViabilityPolicy,
    ViabilityVerdict,
    assess_viability,
    viability_ranking_key,
)
from analytics.decision.v4_configurations import (
    ConfigurationPosition,
    V4Configuration,
    size_configuration_position,
)

STATUS_ACTION = "ACTION"
STATUS_NO_ACTION = "NO_ACTION"

# Stage identities. Operator-facing and persisted, so they are named for the
# question they answer, never "filtered".
STAGE_DATA = "DATA_INVALID"
STAGE_FAMILY = "STRATEGY_FAMILY_NOT_ALLOWED"
STAGE_LIQUIDITY = "LIQUIDITY_BELOW_PROFILE_FLOOR"
STAGE_ECONOMIC = "ECONOMIC_VIABILITY"
STAGE_MOVE_EDGE = "MOVE_EDGE"
STAGE_CAPITAL = "CAPITAL_INCOMPATIBLE"
STAGE_RISK = "RISK_CAP_EXCEEDED"
STAGE_UNDEFINED_RISK = "UNDEFINED_RISK_NOT_SIZEABLE"

#: The two viability reason codes that are move-edge findings rather than
#: economics. Classification only -- the gate itself is untouched.
_MOVE_EDGE_CODES = frozenset({"NO_MOVE_EDGE_VS_IMPLIED", "INSUFFICIENT_MOVE_EVIDENCE"})


@dataclass(frozen=True)
class SharedCandidate:
    """One candidate of the event-level universe, valued exactly once.

    The same instance is read by all six configurations and never mutated,
    which is what guarantees the six observed identical market evidence.
    """

    candidate_id: str
    strategy: str
    economics: CandidateEconomics
    #: Signed executable entry cash for ONE unit; positive = debit.
    entry_cash_required: Decimal | None
    #: Bounded max loss for ONE unit, in dollars. ``None`` means the payoff
    #: is not bounded below or could not be priced -- never sized, never
    #: guessed at.
    per_contract_max_risk: Decimal | None
    #: Real per-leg quote coverage, for the profile liquidity floor.
    n_legs: int = 0
    n_legs_with_two_sided_quote: int = 0
    #: Set when the candidate could not be valued honestly; such a candidate
    #: is never ranked by any configuration (Section 23).
    data_invalid_reason: str | None = None

    @property
    def data_valid(self) -> bool:
        return self.data_invalid_reason is None


@dataclass(frozen=True)
class ConfigurationRejection:
    candidate_id: str
    stage: str
    detail: str


@dataclass(frozen=True)
class ConfigurationDiagnostics:
    """Where one configuration's universe went. The counts sum to
    ``universe_count`` by construction -- each candidate is attributed to the
    first stage that refused it."""

    universe_count: int = 0
    data_invalid_count: int = 0
    strategy_not_permitted_count: int = 0
    liquidity_rejected_count: int = 0
    economic_rejected_count: int = 0
    move_edge_rejected_count: int = 0
    capital_rejected_count: int = 0
    risk_rejected_count: int = 0
    rankable_count: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "universe_count": self.universe_count,
            "data_invalid_count": self.data_invalid_count,
            "strategy_not_permitted_count": self.strategy_not_permitted_count,
            "liquidity_rejected_count": self.liquidity_rejected_count,
            "economic_rejected_count": self.economic_rejected_count,
            "move_edge_rejected_count": self.move_edge_rejected_count,
            "capital_rejected_count": self.capital_rejected_count,
            "risk_rejected_count": self.risk_rejected_count,
            "rankable_count": self.rankable_count,
        }

    @property
    def accounted_for(self) -> int:
        return (
            self.data_invalid_count
            + self.strategy_not_permitted_count
            + self.liquidity_rejected_count
            + self.economic_rejected_count
            + self.move_edge_rejected_count
            + self.capital_rejected_count
            + self.risk_rejected_count
            + self.rankable_count
        )


@dataclass(frozen=True)
class ConfigurationDecision:
    """One configuration's own answer, independent of the other five."""

    configuration: V4Configuration
    status: str
    selected_candidate_id: str | None = None
    rank: int | None = None
    position: ConfigurationPosition | None = None
    no_action_reason: str | None = None
    diagnostics: ConfigurationDiagnostics = field(default_factory=ConfigurationDiagnostics)
    rejections: tuple[ConfigurationRejection, ...] = ()
    ranked_candidate_ids: tuple[str, ...] = ()
    ranking_version: str = CONFIG_RANKING_VERSION
    selection_explanation: str | None = None


def _classify_verdict(verdict: ViabilityVerdict) -> str | None:
    """Which stage a failed absolute-gate verdict belongs to. Returns None
    when the candidate passed."""
    if verdict.acceptable:
        return None
    if any(code in _MOVE_EDGE_CODES for code in verdict.reason_codes):
        # A move-edge finding is reported as such only when it is the ONLY
        # thing wrong; a candidate with negative median economics AND no move
        # edge is an economics refusal, because fixing the edge would not
        # make it takeable.
        if all(code in _MOVE_EDGE_CODES for code in verdict.reason_codes):
            return STAGE_MOVE_EDGE
    return STAGE_ECONOMIC


def _liquidity_ok(candidate: SharedCandidate, configuration: V4Configuration) -> bool:
    """The profile's EXISTING chain-coverage floor, applied to this
    candidate's own contracts.

    Phase 1 never applied a liquidity floor per configuration at all. This
    adds no new number: the thresholds are ``MIN_BID_ASK_COVERAGE`` as
    already released (Conservative 0.80, Moderate 0.40, Aggressive none),
    read through the same ``meets_liquidity_gate`` the rest of the product
    uses. A candidate with no legs recorded is not judged -- an absent
    measurement must not read as a passing one, nor as a failing one, so it
    is treated as unmeasured and left to the other gates.
    """
    if MIN_BID_ASK_COVERAGE[configuration.risk_profile] is None:
        return True
    if candidate.n_legs <= 0:
        return True
    return meets_liquidity_gate(
        configuration.risk_profile,
        candidate.n_legs_with_two_sided_quote,
        candidate.n_legs,
    )


def decide_for_configuration(
    universe: list[SharedCandidate],
    configuration: V4Configuration,
    *,
    verdicts: dict[str, ViabilityVerdict],
) -> ConfigurationDecision:
    """This configuration's own decision over the shared valued universe.

    ``verdicts`` is the event-level absolute-gate result, computed ONCE and
    passed in: the gate is configuration-independent by design, and
    recomputing it six times would be six chances to diverge.
    """
    counts = {
        "data_invalid_count": 0,
        "strategy_not_permitted_count": 0,
        "liquidity_rejected_count": 0,
        "economic_rejected_count": 0,
        "move_edge_rejected_count": 0,
        "capital_rejected_count": 0,
        "risk_rejected_count": 0,
    }
    rejections: list[ConfigurationRejection] = []
    eligible: list[SharedCandidate] = []

    def reject(candidate: SharedCandidate, stage: str, key: str, detail: str) -> None:
        counts[key] += 1
        rejections.append(ConfigurationRejection(candidate.candidate_id, stage, detail))

    for candidate in universe:
        if not candidate.data_valid:
            reject(
                candidate,
                STAGE_DATA,
                "data_invalid_count",
                candidate.data_invalid_reason or "could not be valued honestly",
            )
            continue

        if not is_category_allowed_for_profile(candidate.strategy, configuration.risk_profile):
            reject(
                candidate,
                STAGE_FAMILY,
                "strategy_not_permitted_count",
                f"{configuration.risk_profile.value.title()} does not allow "
                f"{candidate.strategy.replace('_', ' ')}",
            )
            continue

        if not _liquidity_ok(candidate, configuration):
            floor = MIN_BID_ASK_COVERAGE[configuration.risk_profile]
            reject(
                candidate,
                STAGE_LIQUIDITY,
                "liquidity_rejected_count",
                f"{candidate.n_legs_with_two_sided_quote} of {candidate.n_legs} legs carry a "
                f"two-sided market, below {configuration.risk_profile.value.title()}'s "
                f"{floor} floor",
            )
            continue

        verdict = verdicts.get(candidate.candidate_id)
        stage = _classify_verdict(verdict) if verdict is not None else STAGE_ECONOMIC
        if stage is not None:
            detail = (
                "; ".join(verdict.detail)
                if verdict is not None and verdict.detail
                else "refused by the absolute economic viability gate"
            )
            key = (
                "move_edge_rejected_count"
                if stage == STAGE_MOVE_EDGE
                else "economic_rejected_count"
            )
            reject(candidate, stage, key, detail)
            continue

        risk = candidate.per_contract_max_risk
        if risk is None:
            reject(
                candidate,
                STAGE_UNDEFINED_RISK,
                "risk_rejected_count",
                "no bounded maximum loss could be computed, so the structure cannot be "
                "sized against a fixed risk cap",
            )
            continue

        debit = max(candidate.entry_cash_required or Decimal(0), Decimal(0))
        if debit > configuration.capital_base:
            reject(
                candidate,
                STAGE_CAPITAL,
                "capital_rejected_count",
                f"entry costs ${debit:,.2f}, above {configuration.label}'s "
                f"${configuration.capital_base:,.0f} capital base",
            )
            continue

        if risk > configuration.max_risk_dollars:
            reject(
                candidate,
                STAGE_RISK,
                "risk_rejected_count",
                f"one contract risks ${risk:,.2f}, above {configuration.label}'s "
                f"${configuration.max_risk_dollars:,.2f} cap "
                f"({configuration.max_risk_utilization_pct}% of "
                f"${configuration.capital_base:,.0f})",
            )
            continue

        eligible.append(candidate)

    ordered = sorted(
        eligible,
        key=lambda c: (viability_ranking_key(c.economics), _stable_tiebreak(c.candidate_id)),
        reverse=True,
    )
    diagnostics = ConfigurationDiagnostics(
        universe_count=len(universe), rankable_count=len(ordered), **counts
    )

    if not ordered:
        return ConfigurationDecision(
            configuration=configuration,
            status=STATUS_NO_ACTION,
            no_action_reason=_no_action_reason(configuration, diagnostics, rejections),
            diagnostics=diagnostics,
            rejections=tuple(rejections),
        )

    winner = ordered[0]
    position = size_configuration_position(
        configuration,
        candidate_id=winner.candidate_id,
        per_contract_entry_cash=winner.entry_cash_required or Decimal(0),
        per_contract_max_risk=winner.per_contract_max_risk or Decimal(0),
    )
    return ConfigurationDecision(
        configuration=configuration,
        status=STATUS_ACTION,
        selected_candidate_id=winner.candidate_id,
        rank=1,
        position=position,
        diagnostics=diagnostics,
        rejections=tuple(rejections),
        ranked_candidate_ids=tuple(c.candidate_id for c in ordered),
        selection_explanation=_selection_explanation(configuration, winner, ordered, position),
    )


def _stable_tiebreak(candidate_id: str) -> tuple[int, ...]:
    """Deterministic last resort when every ranking dimension ties, so the
    same universe always produces the same winner. Ascending candidate_id
    under ``reverse=True``."""
    return tuple(-ord(c) for c in candidate_id)


def _selection_explanation(
    configuration: V4Configuration,
    winner: SharedCandidate,
    ordered: list[SharedCandidate],
    position: ConfigurationPosition,
) -> str:
    median = winner.economics.median_return
    runner_up = ordered[1] if len(ordered) > 1 else None
    parts = [
        f"{configuration.label} ranked {len(ordered)} eligible structures and took "
        f"{winner.strategy.replace('_', ' ')}"
    ]
    if median is not None:
        parts.append(f"on a modeled T+1 median of {median:.2%}")
    if runner_up is not None and runner_up.economics.median_return is not None:
        parts.append(
            f"ahead of {runner_up.strategy.replace('_', ' ')} at "
            f"{runner_up.economics.median_return:.2%}"
        )
    parts.append(
        f"sized at {position.quantity} contract(s), ${position.capital_used:,.2f} committed, "
        f"${position.max_risk_used:,.2f} at risk against a "
        f"${configuration.max_risk_dollars:,.2f} cap"
    )
    return ", ".join(parts) + "."


def _no_action_reason(
    configuration: V4Configuration,
    diagnostics: ConfigurationDiagnostics,
    rejections: list[ConfigurationRejection],
) -> str:
    """Names the stage that actually emptied this configuration's set, and
    quotes one real refusal from it. A summary that only counted would leave
    an operator unable to tell a capital problem from an economics one."""
    if diagnostics.universe_count == 0:
        return f"{configuration.label}: no candidate universe was constructed for this event"
    stage_counts: dict[str, int] = {}
    for rejection in rejections:
        stage_counts[rejection.stage] = stage_counts.get(rejection.stage, 0) + 1
    if not stage_counts:
        return f"{configuration.label}: no candidate survived, with no stage recorded"
    dominant = max(stage_counts, key=lambda s: stage_counts[s])
    example = next(r for r in rejections if r.stage == dominant)
    return (
        f"{configuration.label}: none of {diagnostics.universe_count} candidates was eligible "
        f"(most common refusal {dominant}, {stage_counts[dominant]} of them). {example.detail}"
    )


def decide_all_configurations(
    universe: list[SharedCandidate],
    configurations: tuple[V4Configuration, ...],
    *,
    evidence: MoveEvidence,
    policy: ViabilityPolicy | None = None,
) -> list[ConfigurationDecision]:
    """All six configurations over one shared universe.

    The absolute gate is evaluated once per candidate here and handed to each
    configuration, so six configurations cost one gate evaluation and, higher
    up, one market-data acquisition.
    """
    policy = policy or DEFAULT_POLICY
    verdicts = {
        c.candidate_id: assess_viability(c.economics, evidence, policy)
        for c in universe
        if c.data_valid
    }
    return [
        decide_for_configuration(universe, configuration, verdicts=verdicts)
        for configuration in configurations
    ]


__all__ = [
    "STATUS_ACTION",
    "STATUS_NO_ACTION",
    "ConfigurationDecision",
    "ConfigurationDiagnostics",
    "ConfigurationRejection",
    "SharedCandidate",
    "decide_all_configurations",
    "decide_for_configuration",
]
