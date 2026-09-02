"""V4.4B -- T+1 executable-outcome candidate ranking.

THE OBJECTIVE CORRECTION. V3 selected strategies largely on
expiration-payoff properties (max profit, max loss, payoff-shape
breakevens) but was benchmarked on *next-day liquidation*. That is an
objective mismatch: a structure can look excellent at expiration and be
economically poor one trading day later, which is the only horizon the
benchmark ever measures. V4.4B ranks against the horizon the benchmark
actually settles on -- pre-earnings entry, first post-earnings trading
day exit, executable bid/ask economics -- by consuming V4.4A's T+1
scenario valuation surface as its primary economic input.

WHAT THIS IS NOT
  * Not an official decision engine. Nothing here is wired into the V3
    scheduler, the official decision pipeline, entry capture, or
    settlement capture (see tests/test_v4_4b_isolation.py, which asserts
    that structurally).
  * Not fitted to anything. No weight, threshold, or band in this module
    was chosen by looking at realized V3 outcomes. There is no training,
    no optimization, and no realized-outcome input anywhere in this
    file's signatures.
  * Not a probability. See "TERMINOLOGY" below.

ARCHITECTURE: TWO STAGES, THEN A BANDED LEXICOGRAPHIC ORDER

Stage 1 -- VALIDITY (data honesty only).
    Answers "can this candidate be valued honestly at all?" -- never
    "is it economically good?". A candidate missing a required
    executable side is a fundamentally different thing from a candidate
    that is simply unattractive, and collapsing both to score=0 (V3's
    habit) destroys that distinction. Only RANKABLE candidates enter
    stage 2; everything else is reported with its reason and is never
    silently dropped.

    Deliberately NOT a validity concern: semantic contradiction. That is
    an economic judgement, not a data problem, so it is handled in stage
    2 as a floor (see below) rather than an exclusion.

Stage 2 -- BANDED LEXICOGRAPHIC ORDER.
    Section 15 warns against repeating V3's 100-point weighted sum of
    many weak components, where a fabricated weight silently traded a
    severe failure mode against a trivial convenience. Four architectures
    were considered (lexicographic, Pareto + tie-break, normalized
    multi-objective, explicit-risk-aversion utility). This module uses a
    LEXICOGRAPHIC order over BANDED dimensions, because:

      * it is auditable -- "why did #1 beat #2" is always answerable by
        naming the single highest dimension on which they differ, with
        no weight arithmetic to reconstruct;
      * it makes the risk-aversion assumption EXPLICIT rather than
        burying it in coefficients: a lower dimension can never
        compensate a higher one. That is a real, opinionated modelling
        choice, and it is stated here rather than hidden;
      * banding stops the brittleness that makes naive lexicographic
        ranking useless -- without it, a 0.0001 difference in one
        dimension would dominate everything below it. Values are
        quantized into coarse, round bands, so only *materially*
        different candidates separate at a level and genuinely-comparable
        ones fall through to the next dimension.

    Order (Section 16's proposed hierarchy, adopted after the critical
    audit recorded in the V4.4B methodology doc):

      1. semantic compatibility band   (V4.2)
      2. downside band                 (worst executable T+1 outcome)
      3. T+1 economic band             (median executable T+1 outcome)
      4. robustness band               (positive-scenario coverage)
      5. execution-quality band        (real per-leg spread friction)
      6. capital-efficiency band       (standardized capital used)
      7. deterministic tie-break       (stable identifiers, never random)

    Semantic contradiction is a FLOOR, not a filter (Section 5): a
    contradictory candidate still ranks, still shows its economics, and
    is still fully explained -- but because semantics is dimension 1, it
    can never outrank a non-contradictory candidate no matter how
    attractive its economics look. This is exactly the V3 failure mode
    that put LONG_VOL views into long_call_butterflies because the
    structure happened to be a net debit.

TERMINOLOGY (Section 7, 24, 25 -- non-negotiable)
    The scenario grid has NO calibrated probability mass: 21 deterministic
    scenarios are not 21 equally-likely futures. Therefore this module
    never produces, and must never be described as producing, an
    "expected return", a "probability of profit", a "win rate", or a
    "confidence". It produces an ORDER, plus the banded key that order
    was derived from. There is deliberately no single composite scalar
    score at all -- the hierarchy does not need one, and inventing one
    would invite exactly the false-precision reading this phase exists to
    remove.

MARKET-DATA PROVENANCE (Section 10, 34)
    Production TWS data is currently DELAYED. Provenance and quality
    ride along on every candidate and are surfaced in the explanation;
    delayed data is neither silently rewarded nor silently penalized,
    because no documented policy for doing either exists yet.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from analytics.decision.v4_compatibility import (
    CONTRADICTION,
    SemanticCompatibilityResult,
    compatibility_tier,
)
from analytics.decision.v4_t1_pricing import (
    T1CandidateDistributionSummary,
    T1ScenarioResult,
)
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext

# --------------------------------------------------------------------------
# Version (Section 38). Frozen for this phase: once historical replay has
# been run against this string, changing ranking behavior REQUIRES a new
# version, never a silent edit under the same name.
# --------------------------------------------------------------------------

RANKING_VERSION = "v4-4b-t1-executable-ranking-v1"


# --------------------------------------------------------------------------
# Stage 1 -- candidate validity vocabulary (Section 13).
# --------------------------------------------------------------------------

CandidateStatus = Literal[
    "RANKABLE",
    "UNCONSTRUCTABLE",
    "QUOTE_INCOMPLETE",
    "MISSING_IV",
    "INSUFFICIENT_EXPECTED_MOVE_EVIDENCE",
    "CANNOT_VALUE_HONESTLY",
    "CAPITAL_INCOMPATIBLE",
]

#: Statuses that mean "we genuinely could not measure this candidate",
#: as distinct from "we measured it and it is unattractive". Never
#: collapsed into a zero score.
NON_RANKABLE_STATUSES: frozenset[str] = frozenset(
    {
        "UNCONSTRUCTABLE",
        "QUOTE_INCOMPLETE",
        "MISSING_IV",
        "INSUFFICIENT_EXPECTED_MOVE_EVIDENCE",
        "CANNOT_VALUE_HONESTLY",
        "CAPITAL_INCOMPATIBLE",
    }
)


# --------------------------------------------------------------------------
# Banding (Section 15). Every band width below is a round, coarse,
# deliberately un-tuned number, declared HEURISTIC_UNCALIBRATED in the
# same spirit as V4.4A's own IV-crush grid. None was chosen by inspecting
# realized outcomes; each exists only to stop trivial numeric noise from
# dominating a lexicographic comparison.
# --------------------------------------------------------------------------

BAND_SEMANTICS_NOTE = "HEURISTIC_UNCALIBRATED -- round band widths, never outcome-fitted"

#: Returns are banded in 5-percentage-point steps of return on
#: standardized capital. Two candidates inside the same 5pp band are
#: treated as economically indistinguishable at that dimension.
RETURN_BAND_WIDTH = Decimal("0.05")

#: Positive-scenario coverage banded in 10-percentage-point steps.
COVERAGE_BAND_WIDTH = Decimal("0.10")

#: Relative bid/ask spread banded in 5-percentage-point steps.
SPREAD_BAND_WIDTH = Decimal("0.05")

#: Capital utilisation banded in 10-percentage-point steps.
CAPITAL_BAND_WIDTH = Decimal("0.10")


def _band(value: Decimal | None, width: Decimal) -> int:
    """Floor ``value`` into a band index. ``None`` -- a genuinely
    unknown measurement -- sorts to the worst possible band rather than
    being silently treated as zero."""
    if value is None:
        return -(10**6)
    return int((value / width).to_integral_value(rounding="ROUND_FLOOR"))


# --------------------------------------------------------------------------
# Execution quality (Section 9). V3's liquidity component was effectively
# binary -- any two-sided quote anywhere earned full marks -- which is
# precisely why an illiquid multi-leg structure could score as well as a
# tight one. This measures the ACTUAL legs the candidate would trade.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ExecutionQuality:
    """Real, per-leg execution observations. Every field is either a
    genuine measurement or ``None`` -- never a fabricated default."""

    n_legs: int
    n_legs_with_two_sided_quote: int
    mean_relative_spread: Decimal | None
    worst_relative_spread: Decimal | None
    all_required_sides_present: bool
    market_data_quality: str | None
    max_leg_timestamp_skew_seconds: Decimal | None
    note: str


def _relative_spread(leg: V4T1LegInput) -> Decimal | None:
    """(ask - bid) / mid. ``None`` when either side is genuinely absent
    or the mid is non-positive -- never guessed from a last price."""
    if leg.entry_bid is None or leg.entry_ask is None:
        return None
    mid = (leg.entry_bid + leg.entry_ask) / 2
    if mid <= 0:
        return None
    return (leg.entry_ask - leg.entry_bid) / mid


def assess_execution_quality(
    legs: tuple[V4T1LegInput, ...],
    max_leg_timestamp_skew_seconds: Decimal | None = None,
) -> ExecutionQuality:
    spreads = [s for s in (_relative_spread(leg) for leg in legs) if s is not None]
    two_sided = sum(
        1 for leg in legs if leg.entry_bid is not None and leg.entry_ask is not None
    )
    required_present = all(leg.entry_executable_price is not None for leg in legs)
    qualities = {leg.market_data_quality for leg in legs if leg.market_data_quality}
    # Mixed quality across legs is itself worth surfacing rather than
    # collapsing to whichever leg happened to be read first.
    quality = (
        next(iter(qualities))
        if len(qualities) == 1
        else ("mixed:" + ",".join(sorted(qualities)) if qualities else None)
    )
    return ExecutionQuality(
        n_legs=len(legs),
        n_legs_with_two_sided_quote=two_sided,
        mean_relative_spread=(sum(spreads, Decimal(0)) / len(spreads)) if spreads else None,
        worst_relative_spread=max(spreads) if spreads else None,
        all_required_sides_present=required_present,
        market_data_quality=quality,
        max_leg_timestamp_skew_seconds=max_leg_timestamp_skew_seconds,
        note=(
            "per-leg measurement over the candidate's own real legs; "
            "never a binary any-quote-present flag"
        ),
    )


# --------------------------------------------------------------------------
# Robustness / pinning (Section 18). Emerges from the scenario surface --
# never a strategy-name penalty. A butterfly is disadvantaged here only
# because its own modeled outcomes collapse outside a narrow region, not
# because it is called a butterfly.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RobustnessDiagnostic:
    positive_scenario_fraction: Decimal | None
    n_positive_underlying_regions: int
    n_underlying_regions: int
    profit_concentrated_in_single_region: bool
    collapses_outside_flat: bool
    #: Distinct from ``profit_concentrated_in_single_region`` on purpose.
    #: A candidate profitable in NO region would otherwise report
    #: "not concentrated", which reads as reassuring when it is the
    #: opposite -- found while replaying real V3 decisions, where every
    #: candidate was unprofitable in every modeled region and the pinning
    #: flag therefore stayed silent. "Never profitable" and "profitable
    #: only if the underlying pins" are different failures and must be
    #: reported as different failures.
    no_profitable_region: bool
    worst_iv_scenario_label: str | None
    note: str


def assess_robustness(results: tuple[T1ScenarioResult, ...]) -> RobustnessDiagnostic:
    valued = [r for r in results if r.return_on_standardized_capital_executable is not None]
    if not valued:
        return RobustnessDiagnostic(
            positive_scenario_fraction=None,
            n_positive_underlying_regions=0,
            n_underlying_regions=0,
            profit_concentrated_in_single_region=False,
            collapses_outside_flat=False,
            no_profitable_region=False,
            worst_iv_scenario_label=None,
            note="no scenario valued -- robustness not measurable",
        )

    positive = [
        r for r in valued if (r.return_on_standardized_capital_executable or Decimal(0)) > 0
    ]
    # Group by underlying-move region: a candidate profitable in many IV
    # scenarios but only one price region is pin-dependent, which is the
    # exact structural risk V3 kept buying.
    regions: dict[str, list[Decimal]] = {}
    for r in valued:
        regions.setdefault(r.underlying_move_label, []).append(
            r.return_on_standardized_capital_executable or Decimal(0)
        )
    positive_regions = [label for label, vals in regions.items() if any(v > 0 for v in vals)]

    worst = min(valued, key=lambda r: r.return_on_standardized_capital_executable or Decimal(0))

    return RobustnessDiagnostic(
        positive_scenario_fraction=Decimal(len(positive)) / Decimal(len(valued)),
        n_positive_underlying_regions=len(positive_regions),
        n_underlying_regions=len(regions),
        profit_concentrated_in_single_region=len(positive_regions) == 1,
        collapses_outside_flat=(len(positive_regions) == 1 and positive_regions[0] == "FLAT"),
        no_profitable_region=len(positive_regions) == 0,
        worst_iv_scenario_label=worst.iv_scenario_label,
        note=(
            "derived entirely from the candidate's own T+1 scenario surface; "
            "no strategy-family-specific rule is applied anywhere"
        ),
    )


# --------------------------------------------------------------------------
# The ranking unit (Section 3) and its result.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RankableCandidate:
    """A fully-specified, executable candidate -- never an abstract
    strategy family. Everything needed to rank it is already resolved:
    real strikes, real sides, real entry quotes, and its own T+1 surface."""

    candidate_id: str
    context: V4T1ValuationContext
    scenario_results: tuple[T1ScenarioResult, ...]
    distribution: T1CandidateDistributionSummary | None
    semantic_compatibility: SemanticCompatibilityResult | None
    entry_cash_required: Decimal | None
    capital_utilisation: Decimal | None
    max_leg_timestamp_skew_seconds: Decimal | None = None


@dataclass(frozen=True)
class RankedCandidate:
    candidate_id: str
    strategy: str
    expiration: str
    status: CandidateStatus
    status_reason: str
    rank: int | None

    # --- ranking dimensions, in hierarchy order ---
    semantic_compatibility: Decimal | None
    semantic_tier: str | None
    worst_executable_return: Decimal | None
    median_executable_return: Decimal | None
    positive_scenario_fraction: Decimal | None
    mean_relative_spread: Decimal | None
    capital_utilisation: Decimal | None

    # --- the exact key this candidate was ordered by ---
    ranking_key: tuple[int, ...] | None

    # --- diagnostics (never ranking inputs unless named above) ---
    execution: ExecutionQuality
    robustness: RobustnessDiagnostic
    entry_cash_required: Decimal | None
    best_executable_return: Decimal | None
    scenario_average_return: Decimal | None
    n_scenarios_valued: int
    market_data_quality: str | None
    data_quality_warnings: tuple[str, ...]
    rationale: str
    ranking_version: str = RANKING_VERSION


# --------------------------------------------------------------------------
# Stage 1 implementation.
# --------------------------------------------------------------------------


def classify_candidate_validity(candidate: RankableCandidate) -> tuple[CandidateStatus, str]:
    """Data honesty only -- deliberately says nothing about whether the
    candidate is economically attractive (Section 13)."""
    legs = candidate.context.legs
    if not legs:
        return "UNCONSTRUCTABLE", "candidate has no legs"

    missing_side = [
        leg.leg_index for leg in legs if leg.entry_executable_price is None
    ]
    if missing_side:
        return (
            "QUOTE_INCOMPLETE",
            "not executable now: required entry side (ASK for buy, BID for sell) missing "
            f"on leg(s) {missing_side}",
        )

    missing_iv = [leg.leg_index for leg in legs if leg.entry_iv is None]
    if missing_iv:
        return (
            "MISSING_IV",
            f"cannot value honestly: no entry IV on leg(s) {missing_iv}, so the T+1 "
            "scenario surface cannot be repriced",
        )

    if not candidate.scenario_results:
        return (
            "INSUFFICIENT_EXPECTED_MOVE_EVIDENCE",
            "no expected-move evidence: the underlying scenario grid could not be built",
        )

    if candidate.distribution is None or candidate.distribution.n_valued == 0:
        return (
            "CANNOT_VALUE_HONESTLY",
            "scenario grid built but no scenario could be valued",
        )

    if candidate.capital_utilisation is not None and candidate.capital_utilisation > 1:
        return (
            "CAPITAL_INCOMPATIBLE",
            "entry cash required exceeds standardized per-decision capital",
        )

    return "RANKABLE", "fully valued on executable entry and T+1 exit economics"


# --------------------------------------------------------------------------
# Stage 2 implementation.
# --------------------------------------------------------------------------


def _semantic_band(compat: SemanticCompatibilityResult | None) -> int:
    """V4.2's own tier thresholds, reused verbatim -- this module invents
    no new semantic scale. An absent evaluation sorts below every
    evaluated candidate rather than being optimistically assumed fine."""
    if compat is None:
        return -1
    return _band(Decimal(str(compat.overall_semantic_compatibility)), Decimal("0.25"))


def build_ranking_key(candidate: RankableCandidate, execution: ExecutionQuality,
                      robustness: RobustnessDiagnostic) -> tuple[int, ...]:
    """The complete, auditable ordering key. Higher is better on every
    element, so a plain descending sort orders candidates -- spread and
    capital are negated because lower is better there."""
    dist = candidate.distribution
    return (
        # 1. semantics -- a contradiction can never be outranked into
        #    first place by attractive economics below it.
        _semantic_band(candidate.semantic_compatibility),
        # 2. downside first (Section 17): the worst modeled executable
        #    T+1 outcome, not an average that can hide it.
        _band(dist.worst_scenario_return if dist else None, RETURN_BAND_WIDTH),
        # 3. central T+1 economics -- median, never a scenario average
        #    masquerading as an expectation (Section 7).
        _band(dist.median_return if dist else None, RETURN_BAND_WIDTH),
        # 4. robustness -- breadth of profitable scenarios.
        _band(robustness.positive_scenario_fraction, COVERAGE_BAND_WIDTH),
        # 5. execution friction -- lower spread is better, hence negated.
        -_band(execution.mean_relative_spread, SPREAD_BAND_WIDTH),
        # 6. capital efficiency -- less standardized capital is better.
        -_band(candidate.capital_utilisation, CAPITAL_BAND_WIDTH),
    )


def _explain(
    candidate: RankableCandidate,
    execution: ExecutionQuality,
    robustness: RobustnessDiagnostic,
    status: CandidateStatus,
    status_reason: str,
) -> str:
    if status != "RANKABLE":
        return f"Not ranked ({status}): {status_reason}."

    compat = candidate.semantic_compatibility
    dist = candidate.distribution
    parts: list[str] = []

    if compat is not None:
        tier = compatibility_tier(compat.overall_semantic_compatibility)
        if compat.overall_semantic_compatibility <= CONTRADICTION:
            parts.append(
                f"Semantic CONTRADICTION with the stated view ({compat.explanation}) -- "
                "floored below every non-contradictory candidate regardless of economics."
            )
        else:
            parts.append(f"Semantic fit {tier} ({compat.explanation}).")
    else:
        parts.append("No semantic evaluation supplied.")

    if dist is not None:
        parts.append(
            f"Worst modeled executable T+1 outcome {dist.worst_scenario_return} "
            f"on standardized capital (scenario {dist.worst_scenario_id}); "
            f"median {dist.median_return} across {dist.n_valued} valued scenarios."
        )
    if robustness.positive_scenario_fraction is not None:
        parts.append(
            f"Profitable in {robustness.positive_scenario_fraction:.0%} of valued scenarios, "
            f"across {robustness.n_positive_underlying_regions}/"
            f"{robustness.n_underlying_regions} underlying-move regions"
            + (
                " -- NOT profitable in ANY modeled underlying-move region."
                if robustness.no_profitable_region
                else " -- profit is concentrated in a single price region, so this depends on "
                "the underlying pinning there."
                if robustness.profit_concentrated_in_single_region
                else "."
            )
        )
    if execution.mean_relative_spread is not None:
        parts.append(
            f"Mean per-leg relative spread {execution.mean_relative_spread:.1%} across "
            f"{execution.n_legs} legs."
        )
    if execution.market_data_quality:
        parts.append(f"Market data: {execution.market_data_quality}.")
    return " ".join(parts)


def _data_quality_warnings(
    candidate: RankableCandidate, execution: ExecutionQuality
) -> tuple[str, ...]:
    warnings: list[str] = []
    if execution.market_data_quality and execution.market_data_quality != "live":
        warnings.append(
            f"market_data_quality={execution.market_data_quality} -- not a live executable "
            "observation"
        )
    if execution.n_legs_with_two_sided_quote < execution.n_legs:
        warnings.append(
            f"only {execution.n_legs_with_two_sided_quote}/{execution.n_legs} legs had a "
            "two-sided quote"
        )
    if candidate.distribution is not None and candidate.distribution.n_valued < len(
        candidate.scenario_results
    ):
        warnings.append(
            f"{candidate.distribution.n_valued}/{len(candidate.scenario_results)} scenarios "
            "could be valued"
        )
    if candidate.max_leg_timestamp_skew_seconds is not None and (
        candidate.max_leg_timestamp_skew_seconds > 0
    ):
        warnings.append(
            f"cross-leg quote timestamp skew {candidate.max_leg_timestamp_skew_seconds}s"
        )
    return tuple(warnings)


def rank_candidates(candidates: list[RankableCandidate]) -> list[RankedCandidate]:
    """Deterministic, total order over ``candidates``.

    Non-rankable candidates are returned too -- with their status, their
    reason, and every diagnostic that could still be measured -- but
    carry ``rank=None`` and never occupy a rank position. They are never
    compared against fully-valued candidates using invented defaults
    (Section 14).
    """
    assessed: list[tuple[RankableCandidate, CandidateStatus, str, ExecutionQuality,
                         RobustnessDiagnostic]] = []
    for candidate in candidates:
        execution = assess_execution_quality(
            candidate.context.legs, candidate.max_leg_timestamp_skew_seconds
        )
        robustness = assess_robustness(candidate.scenario_results)
        status, reason = classify_candidate_validity(candidate)
        assessed.append((candidate, status, reason, execution, robustness))

    rankable = [a for a in assessed if a[1] == "RANKABLE"]
    # Descending by key; ties broken deterministically by candidate_id so
    # the same inputs always produce the same order -- never insertion
    # order, never anything random.
    rankable.sort(key=lambda a: (build_ranking_key(a[0], a[3], a[4]), _neg_id(a[0].candidate_id)),
                  reverse=True)

    ranks: dict[str, int] = {
        a[0].candidate_id: i + 1 for i, a in enumerate(rankable)
    }

    out: list[RankedCandidate] = []
    for candidate, status, reason, execution, robustness in assessed:
        dist = candidate.distribution
        out.append(
            RankedCandidate(
                candidate_id=candidate.candidate_id,
                strategy=str(candidate.context.strategy),
                expiration=str(candidate.context.expiration),
                status=status,
                status_reason=reason,
                rank=ranks.get(candidate.candidate_id),
                semantic_compatibility=(
                    Decimal(str(candidate.semantic_compatibility.overall_semantic_compatibility))
                    if candidate.semantic_compatibility is not None
                    else None
                ),
                semantic_tier=(
                    candidate.semantic_compatibility.tier
                    if candidate.semantic_compatibility is not None
                    else None
                ),
                worst_executable_return=dist.worst_scenario_return if dist else None,
                median_executable_return=dist.median_return if dist else None,
                positive_scenario_fraction=robustness.positive_scenario_fraction,
                mean_relative_spread=execution.mean_relative_spread,
                capital_utilisation=candidate.capital_utilisation,
                ranking_key=(
                    build_ranking_key(candidate, execution, robustness)
                    if status == "RANKABLE"
                    else None
                ),
                execution=execution,
                robustness=robustness,
                entry_cash_required=candidate.entry_cash_required,
                best_executable_return=dist.max_return if dist else None,
                scenario_average_return=dist.scenario_average_return if dist else None,
                n_scenarios_valued=dist.n_valued if dist else 0,
                market_data_quality=execution.market_data_quality,
                data_quality_warnings=_data_quality_warnings(candidate, execution),
                rationale=_explain(candidate, execution, robustness, status, reason),
            )
        )
    # Rankable first (by rank), then non-rankable grouped by status so a
    # reader sees the measurable set before the unmeasurable one.
    out.sort(key=lambda r: (r.rank is None, r.rank or 0, r.status, r.candidate_id))
    return out


def _neg_id(candidate_id: str) -> tuple[int, ...]:
    """Stable descending-sort-safe inverse of a string id, so that when
    every ranking dimension ties, ordering still falls out
    deterministically (ascending candidate_id) under ``reverse=True``."""
    return tuple(-ord(c) for c in candidate_id)


def explain_pairwise(a: RankedCandidate, b: RankedCandidate) -> str:
    """Answers Section 23's question directly: WHY did one candidate beat
    another? Names the single highest dimension on which they differ --
    which is exactly what a lexicographic order makes answerable, and
    what a weighted sum does not."""
    if a.ranking_key is None or b.ranking_key is None:
        return (
            f"{a.candidate_id} and {b.candidate_id} are not directly comparable: "
            f"{a.candidate_id} is {a.status}, {b.candidate_id} is {b.status}."
        )
    dimensions = (
        "semantic compatibility",
        "worst modeled T+1 executable outcome",
        "median T+1 executable outcome",
        "positive-scenario coverage",
        "execution spread friction",
        "capital efficiency",
    )
    for index, name in enumerate(dimensions):
        if a.ranking_key[index] != b.ranking_key[index]:
            better = a if a.ranking_key[index] > b.ranking_key[index] else b
            worse = b if better is a else a
            return (
                f"{better.candidate_id} ranks above {worse.candidate_id} on {name} -- the "
                "highest dimension on which they differ. Every lower dimension is irrelevant "
                "to this comparison by construction."
            )
    return (
        f"{a.candidate_id} and {b.candidate_id} are indistinguishable on every ranking "
        "dimension; order between them is a deterministic identifier tie-break only."
    )
