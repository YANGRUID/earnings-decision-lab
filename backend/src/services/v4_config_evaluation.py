"""Evaluate the six standardized V4 configurations against ONE frozen
market-evidence universe (V4 product consolidation, Sections 14-20, 48-51).

THE ARCHITECTURAL POINT
-----------------------
For a given earnings event and legal window there is exactly one market
observation: one DecisionView, one underlying quote, one deduplicated
option-quote universe, one set of candidate geometries, one T+1 scenario
valuation per candidate. All six configurations rank THAT SAME evidence.

This module is therefore deliberately pure and cheap. It performs no I/O,
issues no provider request, and calls no LLM. Everything expensive already
happened once, upstream, before this is called. Six configurations cost six
in-memory filter-and-sort passes -- not six pipelines.

That is not merely an efficiency concern. Six independent acquisitions
would produce six different quote timestamps against a moving market, and
the six results would no longer be comparable to each other, which is the
entire purpose of running them.

WHAT VARIES PER CONFIGURATION
-----------------------------
Only two gates, both derived from definitions that already existed:

1. **Strategy family** -- Conservative excludes single-leg long calls/puts.
   Delegated to ``risk_profile.is_category_allowed_for_profile`` so this
   file holds no second copy of that rule.
2. **Capital and risk fit** -- the structure must be affordable
   (``entry_cash_required <= capital_base``) and its defined risk must sit
   inside the profile's cap (``max_loss <= max_risk_dollars``). Max loss
   comes from ``analytics.options.payoff.analyze`` -- the same risk maths
   V3's entry capture uses, never a second definition.

Ranking itself is UNCHANGED. Survivors go through the frozen V4.4B
``rank_candidates`` exactly as before; this layer only decides who is
eligible to be ranked. V4.4B ranking v1 is not modified by this work.

NO_ACTION IS A REAL RESULT
--------------------------
A configuration that can afford nothing returns NO_ACTION with the binding
constraint named. That is evidence, not failure, and no rule is relaxed to
manufacture a trade for every configuration (Section 17).
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal

from analytics.decision.risk_profile import is_category_allowed_for_profile
from analytics.decision.v4_4b_ranking import (
    RANKING_VERSION,
    RankableCandidate,
    RankedCandidate,
    rank_candidates,
)
from analytics.decision.v4_configurations import (
    V4_CONFIGURATION_VERSION,
    V4_CONFIGURATIONS,
    V4Configuration,
)
from analytics.options.payoff import Action, OptionLeg, analyze
from models.enums import OptionType

#: Why a candidate was not eligible for a given configuration. These are
#: operator-facing strings and map onto the Operations failure taxonomy
#: (Section 40) -- deliberately specific, never a generic "filtered".
EXCLUDED_STRATEGY_FAMILY = "STRATEGY_FAMILY_NOT_ALLOWED"
EXCLUDED_CAPITAL = "CAPITAL_INSUFFICIENT"
EXCLUDED_RISK_CAP = "RISK_CAP_EXCEEDED"
EXCLUDED_UNDEFINED_RISK = "UNDEFINED_RISK_NOT_SIZEABLE"
EXCLUDED_NOT_PRICEABLE = "NOT_PRICEABLE"


@dataclass(frozen=True)
class CandidateExclusion:
    candidate_id: str
    reason_code: str
    detail: str


@dataclass(frozen=True)
class ConfigurationOutcome:
    """One configuration's frozen result over the shared evidence."""

    configuration: V4Configuration
    status: str  # "RANKED" | "NO_ACTION"
    rank_1_candidate_id: str | None
    ranked: tuple[RankedCandidate, ...]
    eligible_candidate_count: int
    exclusions: tuple[CandidateExclusion, ...]
    no_action_reason: str | None
    configuration_version: str = V4_CONFIGURATION_VERSION
    ranking_version: str = RANKING_VERSION


def _executable_premium(leg) -> Decimal | None:
    """Entry price on the side actually crossed: buy -> ASK, sell -> BID.

    Never a midpoint or last price -- the whole executable-pricing
    convention of this project depends on paying the real side.
    """
    if leg.action == "buy":
        return leg.entry_ask
    return leg.entry_bid


def max_defined_risk(candidate: RankableCandidate) -> Decimal | None:
    """Maximum loss of one unit of the structure, in dollars.

    ``None`` means the payoff is unbounded below (or cannot be priced),
    which no configuration will size -- an undefined-risk structure cannot
    be checked against a fixed risk cap, so it is refused rather than
    guessed at.
    """
    legs: list[OptionLeg] = []
    for leg in candidate.context.legs:
        premium = _executable_premium(leg)
        if premium is None:
            return None
        legs.append(
            OptionLeg(
                option_type=OptionType(leg.right),
                action=Action.BUY if leg.action == "buy" else Action.SELL,
                strike=leg.strike,
                premium=premium,
                quantity=leg.quantity,
            )
        )
    if not legs:
        return None
    analysis = analyze(legs)
    if analysis.max_loss is None:
        return None
    # analyze() works per share; contracts are multiplied by the leg
    # multiplier (100 for standard US equity options).
    multiplier = candidate.context.legs[0].multiplier or Decimal("100")
    return analysis.max_loss * multiplier


def evaluate_configuration(
    candidates: list[RankableCandidate], configuration: V4Configuration
) -> ConfigurationOutcome:
    """Rank the shared candidate universe under one configuration."""
    eligible: list[RankableCandidate] = []
    exclusions: list[CandidateExclusion] = []

    for candidate in candidates:
        family = candidate.context.strategy
        if not is_category_allowed_for_profile(family, configuration.risk_profile):
            exclusions.append(
                CandidateExclusion(
                    candidate.candidate_id,
                    EXCLUDED_STRATEGY_FAMILY,
                    f"{configuration.risk_profile.value.title()} does not allow "
                    f"{family.replace('_', ' ')}",
                )
            )
            continue

        risk = max_defined_risk(candidate)
        if risk is None:
            exclusions.append(
                CandidateExclusion(
                    candidate.candidate_id,
                    EXCLUDED_UNDEFINED_RISK,
                    "Structure has no bounded maximum loss (or a leg is not "
                    "priceable), so it cannot be sized against a fixed risk cap",
                )
            )
            continue

        cash = candidate.entry_cash_required
        if cash is None:
            exclusions.append(
                CandidateExclusion(
                    candidate.candidate_id,
                    EXCLUDED_NOT_PRICEABLE,
                    "No executable entry cost could be computed for this structure",
                )
            )
            continue

        if cash > configuration.capital_base:
            exclusions.append(
                CandidateExclusion(
                    candidate.candidate_id,
                    EXCLUDED_CAPITAL,
                    f"Entry costs ${cash:,.2f}, above the "
                    f"${configuration.capital_base:,.0f} capital base",
                )
            )
            continue

        if risk > configuration.max_risk_dollars:
            # The message names the BINDING constraint (Section 41). Quoting
            # only the capital base is what made the 2026-09-01 PANW refusal
            # read as a budget problem when it was a risk-cap problem.
            exclusions.append(
                CandidateExclusion(
                    candidate.candidate_id,
                    EXCLUDED_RISK_CAP,
                    f"Risk cap exceeded: 1 contract risks ${risk:,.2f}, but "
                    f"{configuration.risk_profile.value.title()} allows "
                    f"${configuration.max_risk_dollars:,.2f} max risk "
                    f"({configuration.max_risk_utilization_pct}% of "
                    f"${configuration.capital_base:,.0f} standardized capital)",
                )
            )
            continue

        eligible.append(candidate)

    # Configuration-relative capital basis (activation phase). The frozen
    # V4.4B ranker classifies a candidate CAPITAL_INCOMPATIBLE when
    # ``capital_utilisation > 1`` and uses utilisation as its last tie-break
    # band. That input is computed by the CALLER; evaluate_shadow_candidate
    # fills it against the V3-era $2,000 standardized capital. Left as-is, a
    # $10,000 configuration could never rank a $3,500 structure it can
    # afford. So the ranker is fed each configuration's own basis. Ranking
    # v1's code, bands and order are untouched -- only its capital INPUT is
    # now the configuration's, which is what "six configurations on shared
    # evidence" means.
    eligible = [
        dataclasses.replace(
            c,
            capital_utilisation=(
                abs(c.entry_cash_required) / configuration.capital_base
                if c.entry_cash_required is not None
                else None
            ),
        )
        for c in eligible
    ]

    if not eligible:
        return ConfigurationOutcome(
            configuration=configuration,
            status="NO_ACTION",
            rank_1_candidate_id=None,
            ranked=(),
            eligible_candidate_count=0,
            exclusions=tuple(exclusions),
            no_action_reason=_summarise_no_action(exclusions, configuration),
        )

    ranked = tuple(rank_candidates(eligible))
    top = next((r for r in ranked if r.rank == 1), None)
    if top is None:
        # Every survivor failed V4.4B's own validity stage (a data-honesty
        # refusal, distinct from a capital/risk refusal).
        return ConfigurationOutcome(
            configuration=configuration,
            status="NO_ACTION",
            rank_1_candidate_id=None,
            ranked=ranked,
            eligible_candidate_count=len(eligible),
            exclusions=tuple(exclusions),
            no_action_reason=(
                "No candidate passed ranking validity -- every affordable structure "
                "was rejected for data-quality reasons"
            ),
        )

    return ConfigurationOutcome(
        configuration=configuration,
        status="RANKED",
        rank_1_candidate_id=top.candidate_id,
        ranked=ranked,
        eligible_candidate_count=len(eligible),
        exclusions=tuple(exclusions),
        no_action_reason=None,
    )


def _summarise_no_action(
    exclusions: list[CandidateExclusion], configuration: V4Configuration
) -> str:
    if not exclusions:
        return "No candidates were generated for this event"
    counts: dict[str, int] = {}
    for exclusion in exclusions:
        counts[exclusion.reason_code] = counts.get(exclusion.reason_code, 0) + 1
    dominant = max(counts, key=lambda code: counts[code])
    example = next(e for e in exclusions if e.reason_code == dominant)
    return (
        f"{configuration.label}: no candidate was eligible "
        f"({len(exclusions)} excluded; most common reason {dominant}). {example.detail}"
    )


def evaluate_all_configurations(
    candidates: list[RankableCandidate],
    configurations: tuple[V4Configuration, ...] = V4_CONFIGURATIONS,
) -> list[ConfigurationOutcome]:
    """All six configurations over one shared, already-frozen universe.

    Pure: the same ``candidates`` list is read six times and never mutated,
    which is what guarantees all six saw identical market evidence.
    """
    return [evaluate_configuration(candidates, config) for config in configurations]
