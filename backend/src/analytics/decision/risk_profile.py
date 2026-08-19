"""Options Decision Engine V3 Part D: a per-decision Risk Profile
(Conservative/Moderate/Aggressive), distinct from the global
StrategyRiskPreference app setting (models/enums.py, services/
provider_settings.py) that this replaces as the primary mechanism for AI
Decision generation -- see services/decision_engine.py::generate_decision's
``risk_profile`` parameter. The global setting still exists (shown/edited
in Settings -> Data Providers) and now doubles as the *default* Risk
Profile when a caller doesn't explicitly choose one for a given decision
(see ``default_risk_profile_from_preference`` below), so existing
deployments that never touch the new per-decision selector see unchanged
behavior.

Every tier below must produce a REAL behavioral difference -- eligibility,
a liquidity gate, or a default risk-cap utilization -- never a cosmetic
label. See the module-level constants for the exact, documented numbers.
"""

from decimal import Decimal

from analytics.options.strategy_candidates import StrategyCandidate, StrategyCategory
from models.enums import RiskProfile, StrategyRiskPreference

# Single-leg long calls/puts have a materially different theta/gamma
# profile than a defined-risk spread -- Conservative excludes them (same
# category set StrategyRiskPreference.DEFINED_RISK_ONLY already excludes;
# reused here rather than re-derived so the two can never disagree about
# which categories count as "single-leg long").
_SINGLE_LEG_LONG_CATEGORIES = frozenset({StrategyCategory.LONG_CALL, StrategyCategory.LONG_PUT})

# Minimum fraction of the real chain's contracts that must carry an
# actual two-sided (bid AND ask) market for a profile to proceed at all --
# Conservative demands the chain-quality threshold this project already
# calls "good" (see services/options_reconstruction.py::classify_chain_quality);
# Moderate accepts "acceptable"; Aggressive adds no extra gate beyond
# whatever the market-data actionability hard gate already requires
# upstream (still real, still priceable -- just no additional liquidity
# floor on top of that).
MIN_BID_ASK_COVERAGE: dict[RiskProfile, Decimal | None] = {
    RiskProfile.CONSERVATIVE: Decimal("0.80"),
    RiskProfile.MODERATE: Decimal("0.40"),
    RiskProfile.AGGRESSIVE: None,
}

# Default max-risk-utilization (as a percent of trade_budget) applied when
# the caller gives a trade_budget but no explicit risk_cap -- concrete
# numbers picked from the middle of each band the product spec suggested
# (Conservative 10-20%, Moderate 20-35%, Aggressive 35-60%), not blindly
# adopted verbatim: sized so adjacent tiers are clearly distinct multiples
# of each other (15/30/50) rather than edge-adjacent values that would
# barely change behavior. A caller-supplied risk_cap always overrides this
# -- these are defaults, never a ceiling the owner can't raise or lower.
DEFAULT_MAX_RISK_UTILIZATION_PCT: dict[RiskProfile, Decimal] = {
    RiskProfile.CONSERVATIVE: Decimal("15"),
    RiskProfile.MODERATE: Decimal("30"),
    RiskProfile.AGGRESSIVE: Decimal("50"),
}

_PREFERENCE_TO_PROFILE: dict[StrategyRiskPreference, RiskProfile] = {
    StrategyRiskPreference.DEFINED_RISK_ONLY: RiskProfile.CONSERVATIVE,
    StrategyRiskPreference.ALLOW_SINGLE_LEG_LONG: RiskProfile.MODERATE,
    StrategyRiskPreference.ADVANCED_ALLOW_UNCOVERED_SHORT: RiskProfile.AGGRESSIVE,
}


def default_risk_profile_from_preference(preference: StrategyRiskPreference) -> RiskProfile:
    """Maps the legacy global StrategyRiskPreference setting to a Risk
    Profile, used only as the *default* when a decision request doesn't
    explicitly choose one -- see generate_decision."""
    return _PREFERENCE_TO_PROFILE[preference]


def filter_candidates_by_risk_profile(
    candidates: list[StrategyCandidate], profile: RiskProfile
) -> list[StrategyCandidate]:
    """Conservative excludes single-leg long calls/puts (a materially
    different risk/theta profile than a spread -- Part 20). Moderate and
    Aggressive allow every category this project currently generates
    (spreads, condors, butterflies, single-leg long -- Part 21/22); there
    is no uncovered-short category yet for Aggressive to additionally
    restrict."""
    if profile == RiskProfile.CONSERVATIVE:
        return [c for c in candidates if c.category not in _SINGLE_LEG_LONG_CATEGORIES]
    return list(candidates)


def meets_liquidity_gate(
    profile: RiskProfile, bid_ask_contract_count: int, contract_count: int
) -> bool:
    """Whether the real chain's overall bid/ask coverage clears this
    profile's liquidity floor -- Conservative rejects a poor-quote chain
    outright (Part 20: "Normally reject: very poor bid/ask") rather than
    silently ranking candidates built from it; Aggressive has no
    additional floor beyond the market-data actionability gate every
    profile is already subject to upstream."""
    threshold = MIN_BID_ASK_COVERAGE[profile]
    if threshold is None:
        return True
    if contract_count == 0:
        return False
    coverage = Decimal(bid_ask_contract_count) / Decimal(contract_count)
    return coverage >= threshold


def default_max_risk_utilization_pct(profile: RiskProfile) -> Decimal:
    """The default risk_cap (as a percent of trade_budget) applied when a
    decision request supplies trade_budget but no explicit risk_cap --
    see services/decision_engine.py. Always overridable by an explicit
    risk_cap; never silently narrows a caller's own choice."""
    return DEFAULT_MAX_RISK_UTILIZATION_PCT[profile]
