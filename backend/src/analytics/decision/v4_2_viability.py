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

LONG_MOVE_STRATEGIES = frozenset({"long_straddle", "long_strangle"})
SHORT_MOVE_STRATEGIES = frozenset(
    {"iron_butterfly", "iron_condor", "long_call_butterfly",
     "put_credit_spread", "call_credit_spread"}
)


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


@dataclass(frozen=True)
class MoveEvidence:
    """What Python -- never the language model -- computed about the move.

    ``expected_abs_move_pct`` is None when no quantitative expected move
    could be derived from evidence (e.g. no historical post-earnings move
    distribution). That is a refusal to guess, and the gate treats it as
    such rather than falling back on a qualitative label.
    """

    implied_move_pct: Decimal | None
    expected_abs_move_pct: Decimal | None
    historical_sample_n: int = 0

    @property
    def edge_ratio(self) -> Decimal | None:
        """expected / implied. >1 favours long-move, <1 short-move."""
        if (
            self.implied_move_pct is None
            or self.expected_abs_move_pct is None
            or self.implied_move_pct <= 0
        ):
            return None
        return self.expected_abs_move_pct / self.implied_move_pct


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


def move_edge_verdict(
    strategy: str, evidence: MoveEvidence, policy: ViabilityPolicy = DEFAULT_POLICY
) -> tuple[bool, str | None, str | None]:
    """Whether the move exposure this strategy takes is justified against
    what the option market already prices.

    A strategy with no directional move exposure (a vertical, a single
    call/put) is not judged here -- its edge is directional, and this gate
    makes no claim about direction.
    """
    if not policy.require_move_edge:
        return True, None, None
    long_move = strategy in LONG_MOVE_STRATEGIES
    short_move = strategy in SHORT_MOVE_STRATEGIES
    if not (long_move or short_move):
        return True, None, None

    ratio = evidence.edge_ratio
    if ratio is None:
        return (
            False,
            INSUFFICIENT_MOVE_EVIDENCE,
            (
                "no quantitative expected move could be derived "
                f"(historical sample n={evidence.historical_sample_n}); a qualitative "
                "volatility label alone does not establish an edge against the "
                "implied move"
            ),
        )

    margin = policy.move_edge_margin
    if long_move and ratio < (Decimal(1) + margin):
        return (
            False,
            NO_MOVE_EDGE,
            f"long-move structure needs expected/implied > {1 + margin:.2f}, observed {ratio:.2f}",
        )
    if short_move and ratio > (Decimal(1) - margin):
        return (
            False,
            NO_MOVE_EDGE,
            f"short-move structure needs expected/implied < {1 - margin:.2f}, observed {ratio:.2f}",
        )
    return True, None, None


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

    ok, code, why = move_edge_verdict(economics.strategy, evidence, policy)
    if not ok and code is not None:
        reasons.append(code)
        if why:
            detail.append(why)

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
