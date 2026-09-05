"""V4.2 CHALLENGER -- an absolute economic viability gate.

Not enabled in production. v4.1 remains the control methodology; this exists
so the two can be compared on the same frozen evidence.

WHY THIS EXISTS
---------------
The 2026-09-05 forensic audit established, from the production database,
that V4.1 has no absolute economic gate of any kind:

  * ``classify_candidate_validity`` says so in its own docstring -- it is
    "data honesty only" and "deliberately says nothing about whether the
    candidate is economically attractive";
  * ``rank_candidates`` only SORTS, and the decision layer takes rank #1
    whenever at least one candidate is honestly rankable, so NO_ACTION can
    only ever be triggered by missing data, never by bad economics;
  * ``build_ranking_key`` is lexicographic with the semantic band FIRST, so
    a candidate with better modeled economics can never outrank one in a
    higher semantic band.

Across the first 7 natural events, all 7 selected candidates had a negative
modeled median executable T+1 return, and 2 (DOCU, ZS) were selected with
``no_profitable_region`` -- the engine's own valuation said no modeled
scenario made money, best cases -2.12% and -1.86%. In 5 of 7 events a
materially better ex-ante candidate existed in the same rankable set, twice
with a POSITIVE modeled median that was passed over.

WHAT THIS GATE IS, AND IS NOT
-----------------------------
Every threshold below is an ex-ante economic statement, not a number fitted
to realized outcomes. None was selected by checking whether it made a
particular losing trade disappear:

  * ``median <= 0`` -- you do not knowingly open a position your own model
    says loses money at the median. This is the minimal definition of a
    trade worth taking and is not tunable.
  * ``no profitable region`` / ``positive scenario fraction == 0`` -- there
    is no modeled state of the world in which the position profits.
    Tautological.
  * ``worst case`` and ``spread`` bounds -- risk and friction limits,
    expressed as explicit configuration so their sensitivity can be
    reported rather than hidden.

The move-edge requirement answers the audit's other finding: V4.1 maps a
QUALITATIVE volatility label to strategy semantics
(``long_vol -> large_move``) and never compares expected move against what
the option market already implies. A long-move structure is only worth
holding if the expected move exceeds the implied move by a real margin, and
a short-move structure only if it falls below it by one. Where no
quantitative move evidence exists, this gate refuses the structure rather
than accepting the label -- it does not invent an expected move, and it
never asks the language model to produce one.
"""

from dataclasses import dataclass, field
from decimal import Decimal

VIABILITY_GATE_VERSION = "v4_2_viability_gate_v1"
MOVE_EDGE_VERSION = "v4_2_move_edge_v1"

# ---------------------------------------------------------------------------
# Reason codes -- every rejection names itself.
# ---------------------------------------------------------------------------
NO_PROFITABLE_REGION = "NO_PROFITABLE_REGION"
NO_POSITIVE_SCENARIOS = "NO_POSITIVE_SCENARIOS"
NEGATIVE_MEDIAN = "NEGATIVE_MEDIAN_EXECUTABLE_RETURN"
WORST_CASE_UNACCEPTABLE = "WORST_CASE_UNACCEPTABLE"
SPREAD_UNACCEPTABLE = "ROUND_TRIP_SPREAD_UNACCEPTABLE"
SEMANTIC_UNACCEPTABLE = "SEMANTIC_COMPATIBILITY_UNACCEPTABLE"
INSUFFICIENT_MOVE_EVIDENCE = "INSUFFICIENT_MOVE_EVIDENCE"
NO_MOVE_EDGE = "NO_MOVE_EDGE_VS_IMPLIED"
MISSING_ECONOMICS = "MISSING_ECONOMICS"
CAPITAL_INCOMPATIBLE = "CAPITAL_INCOMPATIBLE"
RISK_CAP_EXCEEDED = "RISK_CAP_EXCEEDED"


@dataclass(frozen=True)
class ViabilityPolicy:
    """Explicit, versioned, and reported alongside every verdict.

    Defaults are economic statements, not fitted constants -- see the module
    docstring. ``sensitivity`` runs elsewhere vary these deliberately and
    report the effect on ACTION/NO_ACTION counts, never on realized P&L.
    """

    #: A trade must not be modeled to lose at the median.
    min_median_return: Decimal = Decimal("0")
    #: There must exist at least one modeled scenario in which it profits.
    min_positive_scenario_fraction: Decimal = Decimal("0")
    #: Cap on the modeled worst case as a fraction of standardized capital.
    #: 0.35 is a risk statement (a third of the standardized capital), set
    #: deliberately loose so the median gate, not this, does the work.
    max_worst_case_loss: Decimal = Decimal("0.35")
    #: Mean relative spread above which round-trip friction dominates. At
    #: 0.25 a position gives up roughly a quarter of mid to trade in and
    #: out, which no modeled median in this universe covers.
    max_mean_relative_spread: Decimal = Decimal("0.25")
    #: Contradictory semantics are never acceptable.
    min_semantic_compatibility: Decimal = Decimal("0.25")
    #: A long-move structure needs the expected move to exceed implied by
    #: this margin; a short-move structure needs it below by the same.
    #: 0.20 = "materially", not a rounding difference.
    move_edge_margin: Decimal = Decimal("0.20")
    #: Whether a move-exposed structure must demonstrate a quantitative edge
    #: against the implied move at all. Turning this off isolates the pure
    #: economic gate in sensitivity reporting; it is never off in the
    #: challenger's own default policy, because accepting a qualitative
    #: volatility label as an edge is one of the defects this gate exists
    #: to correct.
    require_move_edge: bool = True


DEFAULT_POLICY = ViabilityPolicy()

# Move-edge applicability is derived from the project's OWN payoff-shape
# taxonomy (analytics/decision/v4_strategy_semantics.py), never from a list of
# strategy names kept here. That matters for two reasons: a name list is a
# second source of truth that silently rots when a family is added, and a gate
# that names strategies is one edit away from becoming a preference for or
# against particular families -- which this challenger must never contain.
#
#   two_sided_convex              -> profits from MAGNITUDE in either
#                                    direction; the long-move test applies.
#   range_credit / tent_pinning   -> profit from the move staying small; the
#                                    short-move test applies.
#   single_sided_convex           -> a long call/put needs a DIRECTIONAL move
#                                    past its own breakeven. A two-sided
#                                    magnitude test is the wrong instrument,
#                                    and the modeled-median gate already
#                                    prices its breakeven and IV crush.
#   vertical_bounded_directional  -> bounded, threshold-shaped payoffs
#                                    (including credit verticals, whose risk
#                                    is a directional move against them, not
#                                    a large move in either direction).
#
# The last two are NOT_APPLICABLE, which is a statement about economics, not
# an exemption: they still face every other gate.
LONG_MOVE_SHAPES = frozenset({"two_sided_convex"})
SHORT_MOVE_SHAPES = frozenset({"range_credit", "tent_pinning"})


def move_exposure_for(strategy: str) -> str | None:
    """"long" | "short" | None (not move-magnitude exposed).

    Returns None for an unrecognised strategy rather than guessing -- an
    unknown family is not quietly assumed to be direction-neutral.
    """
    from analytics.decision.v4_strategy_semantics import (  # noqa: PLC0415
        get_strategy_semantics,
    )
    from analytics.options.strategy_candidates import StrategyCategory  # noqa: PLC0415

    try:
        semantics = get_strategy_semantics(StrategyCategory(strategy))
    except (ValueError, KeyError):
        return None
    if semantics.payoff_shape in LONG_MOVE_SHAPES:
        return "long"
    if semantics.payoff_shape in SHORT_MOVE_SHAPES:
        return "short"
    return None


@dataclass(frozen=True)
class CandidateEconomics:
    """The ex-ante facts this gate reads. Every one is already persisted on
    ``v4_shadow_candidate`` -- nothing here needs new market data."""

    candidate_id: str
    strategy: str
    median_return: Decimal | None
    worst_return: Decimal | None
    best_return: Decimal | None
    positive_scenario_fraction: Decimal | None
    no_profitable_region: bool | None
    semantic_compatibility: Decimal | None
    mean_relative_spread: Decimal | None


MOVE_EDGE_PASS = "PASS"
MOVE_EDGE_FAIL = "FAIL"
MOVE_EDGE_INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
MOVE_EDGE_NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class MoveEvidence:
    """What Python -- never the language model -- knows about the move.

    ``distribution`` is the point-in-time historical distribution; it carries
    its own sample size, quality tier and timing provenance, so this object
    cannot present thin evidence as strong.
    """

    implied_move_pct: Decimal | None
    distribution: object | None = None

    @property
    def expected_abs_move_pct(self) -> Decimal | None:
        """The central tendency of the historical distribution.

        The MEDIAN absolute move, deliberately, rather than an exceedance
        proportion: at the sample sizes available (10-48 per company) a
        proportion carries a standard error of roughly 7-15 percentage
        points, while the median is stable. Exceedance is reported alongside
        as a diagnostic, never as the gate.
        """
        return getattr(self.distribution, "median_abs_move_pct", None)

    @property
    def sample_n(self) -> int:
        return int(getattr(self.distribution, "sample_n", 0) or 0)

    @property
    def quality(self) -> str | None:
        return getattr(self.distribution, "quality", None)

    @property
    def edge_ratio(self) -> Decimal | None:
        """expected / implied. Above 1 favours a long-move structure, below
        1 a short-move one."""
        expected = self.expected_abs_move_pct
        if expected is None or self.implied_move_pct is None or self.implied_move_pct <= 0:
            return None
        return expected / self.implied_move_pct


@dataclass(frozen=True)
class MoveEdgeResult:
    """Section 19 -- an explicit diagnostic, not a bare boolean."""

    status: str
    exposure: str | None
    implied_move_pct: Decimal | None
    expected_abs_move_pct: Decimal | None
    edge_ratio: Decimal | None
    threshold: Decimal | None
    sample_n: int
    quality: str | None
    exceedance_of_implied: Decimal | None
    explanation: str
    version: str = MOVE_EDGE_VERSION

    @property
    def blocking(self) -> bool:
        return self.status in (MOVE_EDGE_FAIL, MOVE_EDGE_INSUFFICIENT)

    @property
    def reason_code(self) -> str | None:
        if self.status == MOVE_EDGE_FAIL:
            return NO_MOVE_EDGE
        if self.status == MOVE_EDGE_INSUFFICIENT:
            return INSUFFICIENT_MOVE_EVIDENCE
        return None


def evaluate_move_edge(
    strategy: str, evidence: MoveEvidence, policy: "ViabilityPolicy | None" = None
) -> MoveEdgeResult:
    """Whether the move exposure this structure takes is justified against
    what the option market already prices."""
    policy = policy or DEFAULT_POLICY
    exposure = move_exposure_for(strategy)
    exceedance = None
    if evidence.distribution is not None and evidence.implied_move_pct is not None:
        exceed = getattr(evidence.distribution, "exceedance_frequency", None)
        if callable(exceed):
            exceedance = exceed(evidence.implied_move_pct)

    if exposure is None or not policy.require_move_edge:
        return MoveEdgeResult(
            status=MOVE_EDGE_NOT_APPLICABLE,
            exposure=exposure,
            implied_move_pct=evidence.implied_move_pct,
            expected_abs_move_pct=evidence.expected_abs_move_pct,
            edge_ratio=evidence.edge_ratio,
            threshold=None,
            sample_n=evidence.sample_n,
            quality=evidence.quality,
            exceedance_of_implied=exceedance,
            explanation=(
                "this structure's payoff is not move-magnitude exposed, so a "
                "two-sided move-edge test is not the right instrument for it"
                if exposure is None
                else "move-edge requirement disabled for sensitivity analysis"
            ),
        )

    ratio = evidence.edge_ratio
    margin = policy.move_edge_margin
    threshold = (Decimal(1) + margin) if exposure == "long" else (Decimal(1) - margin)

    if ratio is None:
        return MoveEdgeResult(
            status=MOVE_EDGE_INSUFFICIENT,
            exposure=exposure,
            implied_move_pct=evidence.implied_move_pct,
            expected_abs_move_pct=evidence.expected_abs_move_pct,
            edge_ratio=None,
            threshold=threshold,
            sample_n=evidence.sample_n,
            quality=evidence.quality,
            exceedance_of_implied=exceedance,
            explanation=(
                f"no quantitative expected move could be derived (sample n="
                f"{evidence.sample_n}, quality={evidence.quality}); a qualitative "
                "volatility label alone does not establish an edge against the "
                "implied move"
            ),
        )

    passed = ratio >= threshold if exposure == "long" else ratio <= threshold
    direction = "above" if exposure == "long" else "below"
    return MoveEdgeResult(
        status=MOVE_EDGE_PASS if passed else MOVE_EDGE_FAIL,
        exposure=exposure,
        implied_move_pct=evidence.implied_move_pct,
        expected_abs_move_pct=evidence.expected_abs_move_pct,
        edge_ratio=ratio,
        threshold=threshold,
        sample_n=evidence.sample_n,
        quality=evidence.quality,
        exceedance_of_implied=exceedance,
        explanation=(
            f"{exposure}-move structure requires historical median move "
            f"{direction} implied by {margin:.0%} (ratio {'>=' if exposure == 'long' else '<='} "
            f"{threshold:.2f}); observed {ratio:.2f} from n={evidence.sample_n}"
        ),
    )


@dataclass(frozen=True)
class ViabilityVerdict:
    candidate_id: str
    acceptable: bool
    reason_codes: tuple[str, ...] = ()
    detail: tuple[str, ...] = ()
    gate_version: str = VIABILITY_GATE_VERSION

    @property
    def primary_reason(self) -> str | None:
        return self.reason_codes[0] if self.reason_codes else None


def assess_viability(
    economics: CandidateEconomics,
    evidence: MoveEvidence,
    policy: ViabilityPolicy = DEFAULT_POLICY,
) -> ViabilityVerdict:
    """Absolute, per-candidate. Says nothing about other candidates -- this
    is deliberately NOT a relative ranking, which is exactly what V4.1
    already has and exactly what cannot produce a NO_ACTION."""
    reasons: list[str] = []
    detail: list[str] = []

    if economics.median_return is None or economics.positive_scenario_fraction is None:
        return ViabilityVerdict(
            economics.candidate_id,
            False,
            (MISSING_ECONOMICS,),
            ("candidate could not be valued, so it cannot be judged economically viable",),
        )

    if economics.no_profitable_region or (
        economics.best_return is not None and economics.best_return <= 0
    ):
        reasons.append(NO_PROFITABLE_REGION)
        detail.append(
            f"no modeled scenario profits (best {economics.best_return})"
            if economics.best_return is not None
            else "no modeled scenario profits"
        )

    if economics.positive_scenario_fraction <= policy.min_positive_scenario_fraction:
        reasons.append(NO_POSITIVE_SCENARIOS)
        detail.append(
            f"positive scenario fraction {economics.positive_scenario_fraction} is not above "
            f"{policy.min_positive_scenario_fraction}"
        )

    if economics.median_return <= policy.min_median_return:
        reasons.append(NEGATIVE_MEDIAN)
        detail.append(
            f"modeled median executable T+1 return {economics.median_return} is not above "
            f"{policy.min_median_return}"
        )

    if economics.worst_return is not None and economics.worst_return < -policy.max_worst_case_loss:
        reasons.append(WORST_CASE_UNACCEPTABLE)
        detail.append(
            f"modeled worst case {economics.worst_return} exceeds the "
            f"{policy.max_worst_case_loss} limit on standardized capital"
        )

    if (
        economics.mean_relative_spread is not None
        and economics.mean_relative_spread > policy.max_mean_relative_spread
    ):
        reasons.append(SPREAD_UNACCEPTABLE)
        detail.append(
            f"mean relative spread {economics.mean_relative_spread} exceeds "
            f"{policy.max_mean_relative_spread}; round-trip friction dominates the modeled edge"
        )

    if (
        economics.semantic_compatibility is not None
        and economics.semantic_compatibility < policy.min_semantic_compatibility
    ):
        reasons.append(SEMANTIC_UNACCEPTABLE)
        detail.append(
            f"semantic compatibility {economics.semantic_compatibility} is below "
            f"{policy.min_semantic_compatibility}"
        )

    edge = evaluate_move_edge(economics.strategy, evidence, policy)
    if edge.blocking and edge.reason_code is not None:
        reasons.append(edge.reason_code)
        detail.append(edge.explanation)

    return ViabilityVerdict(
        economics.candidate_id, not reasons, tuple(reasons), tuple(detail)
    )


@dataclass
class ChallengerDecision:
    """One event's V4.2 recommendation, alongside why every candidate that
    did not win was refused."""

    status: str  # "RANKED" | "NO_ACTION"
    selected_candidate_id: str | None = None
    no_action_reason: str | None = None
    gate_version: str = VIABILITY_GATE_VERSION
    move_edge_version: str = MOVE_EDGE_VERSION
    verdicts: list[ViabilityVerdict] = field(default_factory=list)

    @property
    def accepted(self) -> list[ViabilityVerdict]:
        return [v for v in self.verdicts if v.acceptable]


def choose_v4_2_candidate(
    candidates: list[CandidateEconomics],
    evidence: MoveEvidence,
    policy: ViabilityPolicy = DEFAULT_POLICY,
) -> ChallengerDecision:
    """Gate first, then rank -- the order that makes NO_ACTION reachable.

    Among candidates that clear the absolute gate, the best modeled median
    wins, with the worst case breaking ties. Note what is NOT here: semantic
    band no longer dominates economics lexicographically, because the audit
    showed that is precisely what buried positive-median candidates beneath
    negative-median ones. Semantics is a GATE (a contradiction is refused
    outright), not the primary sort key.
    """
    verdicts = [assess_viability(c, evidence, policy) for c in candidates]
    by_id = {c.candidate_id: c for c in candidates}
    accepted = [v for v in verdicts if v.acceptable]

    if not accepted:
        counts: dict[str, int] = {}
        for v in verdicts:
            for code in v.reason_codes:
                counts[code] = counts.get(code, 0) + 1
        summary = ", ".join(
            f"{code} ({n})" for code, n in sorted(counts.items(), key=lambda kv: -kv[1])
        )
        return ChallengerDecision(
            status="NO_ACTION",
            no_action_reason=(
                f"no candidate cleared the absolute economic viability gate: {summary}"
                if summary
                else "no candidate could be assessed"
            ),
            verdicts=verdicts,
        )

    def sort_key(v: ViabilityVerdict):
        econ = by_id[v.candidate_id]
        return (
            econ.median_return or Decimal(0),
            econ.worst_return or Decimal(0),
            econ.positive_scenario_fraction or Decimal(0),
            -(econ.mean_relative_spread or Decimal(0)),
        )

    winner = max(accepted, key=sort_key)
    return ChallengerDecision(
        status="RANKED", selected_candidate_id=winner.candidate_id, verdicts=verdicts
    )


# ---------------------------------------------------------------------------
# Per-configuration outcomes (Sections 40-42).
#
# The six configurations share one evidence package and one market-data
# acquisition -- nothing here re-quotes anything. What differs per
# configuration is capital and risk tolerance, so a candidate that is
# economically viable can still be incompatible with one configuration's
# capital base or defined-risk cap while remaining right for another.
#
# It is therefore a correct outcome for $2K Conservative to return NO_ACTION
# while $10K Moderate actions the same evidence. The six are not slots to be
# filled.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConfigurationConstraints:
    """The per-configuration limits, passed in rather than imported, so this
    module never grows its own copy of the cohort definitions."""

    key: str
    capital_base: Decimal
    max_risk_dollars: Decimal


def assess_configuration_fit(
    economics: CandidateEconomics,
    constraints: ConfigurationConstraints,
    *,
    entry_cash_required: Decimal | None,
    max_loss_dollars: Decimal | None,
) -> tuple[bool, tuple[str, ...], tuple[str, ...]]:
    """Whether a candidate that already cleared the absolute economic gate
    also fits THIS configuration's capital and risk limits."""
    reasons: list[str] = []
    detail: list[str] = []

    if entry_cash_required is not None and entry_cash_required > constraints.capital_base:
        reasons.append(CAPITAL_INCOMPATIBLE)
        detail.append(
            f"entry cash {entry_cash_required} exceeds {constraints.key}'s capital base "
            f"{constraints.capital_base}"
        )

    if max_loss_dollars is not None and max_loss_dollars > constraints.max_risk_dollars:
        reasons.append(RISK_CAP_EXCEEDED)
        detail.append(
            f"defined risk {max_loss_dollars} exceeds {constraints.key}'s cap "
            f"{constraints.max_risk_dollars}"
        )

    return not reasons, tuple(reasons), tuple(detail)


def choose_v4_2_candidate_for_configuration(
    candidates: list[CandidateEconomics],
    evidence: MoveEvidence,
    constraints: ConfigurationConstraints,
    *,
    entry_cash_by_candidate: dict[str, Decimal] | None = None,
    max_loss_by_candidate: dict[str, Decimal] | None = None,
    policy: ViabilityPolicy | None = None,
) -> ChallengerDecision:
    """One configuration's own decision over the SHARED candidate set.

    The absolute economic gate is identical across configurations -- an
    economically bad trade is bad at every size -- and only the capital and
    risk fit differs.
    """
    policy = policy or DEFAULT_POLICY
    entry_cash_by_candidate = entry_cash_by_candidate or {}
    max_loss_by_candidate = max_loss_by_candidate or {}

    verdicts: list[ViabilityVerdict] = []
    for candidate in candidates:
        verdict = assess_viability(candidate, evidence, policy)
        if verdict.acceptable:
            fits, reasons, detail = assess_configuration_fit(
                candidate,
                constraints,
                entry_cash_required=entry_cash_by_candidate.get(candidate.candidate_id),
                max_loss_dollars=max_loss_by_candidate.get(candidate.candidate_id),
            )
            if not fits:
                verdict = ViabilityVerdict(
                    candidate.candidate_id, False, reasons, detail
                )
        verdicts.append(verdict)

    accepted = [v for v in verdicts if v.acceptable]
    by_id = {c.candidate_id: c for c in candidates}
    if not accepted:
        counts: dict[str, int] = {}
        for v in verdicts:
            for code in v.reason_codes:
                counts[code] = counts.get(code, 0) + 1
        summary = ", ".join(
            f"{code} ({n})" for code, n in sorted(counts.items(), key=lambda kv: -kv[1])
        )
        return ChallengerDecision(
            status="NO_ACTION",
            no_action_reason=(
                f"no candidate was viable and compatible with {constraints.key}: {summary}"
                if summary
                else "no candidate could be assessed"
            ),
            verdicts=verdicts,
        )

    def sort_key(v: ViabilityVerdict):
        econ = by_id[v.candidate_id]
        return (
            econ.median_return or Decimal(0),
            econ.worst_return or Decimal(0),
            econ.positive_scenario_fraction or Decimal(0),
            -(econ.mean_relative_spread or Decimal(0)),
        )

    winner = max(accepted, key=sort_key)
    return ChallengerDecision(
        status="RANKED", selected_candidate_id=winner.candidate_id, verdicts=verdicts
    )
