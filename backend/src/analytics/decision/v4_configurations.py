"""The six standardized V4 forward-test configurations (V4 product
consolidation, 2026-09-02).

WHAT THIS IS
------------
V4 is forward-tested under six configurations -- two capital bases
(2,000 / 10,000) crossed with the three existing risk profiles
(Conservative / Moderate / Aggressive). All six evaluate the SAME frozen
market evidence for a given earnings event: one DecisionView, one
underlying observation, one deduplicated option-quote universe, one set of
candidate geometries. Only the capital-fit and risk-policy layers differ.

That sharing is the whole point. Six independent pipelines would mean six
LLM calls, six market-data acquisitions, six different quote timestamps
and 6x the TWS request budget -- and the six results would then not be
comparable, because they would not be observing the same market.

WHERE THE NUMBERS COME FROM
---------------------------
Nothing here invents a risk number. The three risk profiles already exist
and are already behaviorally real -- see analytics/decision/risk_profile.py,
which defines, per profile: a max-risk-utilization default
(15% / 30% / 50%), a chain liquidity floor (0.80 / 0.40 / none), and a
strategy-family filter (Conservative excludes single-leg longs). This
module composes those existing definitions with a capital base; it does
not retune them.

The capital bases are the two the product tests: 2,000 (the existing
benchmark control size, kept so the new cohort remains comparable to the
V3 control) and 10,000 (large enough that structures priced out at 2,000
become reachable -- the PANW long put on 2026-09-01 needed $1,155 against
a $600 Moderate cap at 2,000, and would have fit at 10,000).

NO OUTCOME FITTING
------------------
These six identities were fixed before looking at any realized result, and
no realized V3 or V4 outcome was used to choose the capital bases, the
risk percentages, or the liquidity floors.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from analytics.decision.risk_profile import (
    DEFAULT_MAX_RISK_UTILIZATION_PCT,
    MIN_BID_ASK_COVERAGE,
)
from models.enums import RiskProfile

#: Bumped only when the SET of configurations or their capital/risk
#: identity changes -- never for a code refactor. Frozen on every
#: configuration-specific result so a reader can tell what "Moderate"
#: meant at the time that row was written.
V4_CONFIGURATION_VERSION = "v4-forward-configurations-v1"

CAPITAL_2K = Decimal("2000")
CAPITAL_10K = Decimal("10000")


@dataclass(frozen=True)
class V4Configuration:
    """One of the six forward-test configurations.

    ``key`` is the stable persisted identity. ``capital_base`` and
    ``risk_profile`` are stored as their own columns too, so a later reader
    never has to parse the key string to recover them.
    """

    key: str
    capital_base: Decimal
    risk_profile: RiskProfile
    label: str

    @property
    def max_risk_utilization_pct(self) -> Decimal:
        """Percent of capital_base allowed as maximum defined risk.
        Delegates to the existing per-profile default -- never a second,
        divergent copy of those numbers."""
        return DEFAULT_MAX_RISK_UTILIZATION_PCT[self.risk_profile]

    @property
    def max_risk_dollars(self) -> Decimal:
        """The binding risk constraint in dollars.

        This is the number that actually refuses trades, and the number
        operator-facing messages must quote. Reporting only the capital
        base is what made the 2026-09-01 PANW refusal read as a budget
        problem when it was a risk-cap problem.
        """
        return self.capital_base * self.max_risk_utilization_pct / Decimal("100")

    @property
    def min_bid_ask_coverage(self) -> Decimal | None:
        """This profile's chain liquidity floor, or None for no floor."""
        return MIN_BID_ASK_COVERAGE[self.risk_profile]


def _build(capital: Decimal, capital_label: str, profile: RiskProfile) -> V4Configuration:
    return V4Configuration(
        key=f"v4_{capital_label.lower()}_{profile.value.lower()}",
        capital_base=capital,
        risk_profile=profile,
        label=f"${int(capital):,} {profile.value.title()}",
    )


#: All six, in a stable display order: capital ascending, then risk
#: ascending. Order is part of the contract -- the UI and the comparison
#: view both render in this sequence.
V4_CONFIGURATIONS: tuple[V4Configuration, ...] = tuple(
    _build(capital, label, profile)
    for capital, label in ((CAPITAL_2K, "2K"), (CAPITAL_10K, "10K"))
    for profile in (RiskProfile.CONSERVATIVE, RiskProfile.MODERATE, RiskProfile.AGGRESSIVE)
)

#: The existing benchmark control size/profile, kept as the UI default so
#: the default view stays comparable to the V3 control cohort.
DEFAULT_CONFIGURATION_KEY = "v4_2k_moderate"

_BY_KEY: dict[str, V4Configuration] = {c.key: c for c in V4_CONFIGURATIONS}


def get_configuration(key: str) -> V4Configuration:
    """Resolves a persisted configuration key. Raises rather than
    defaulting: a result whose configuration is unknown must never be
    silently reinterpreted under a different capital or risk policy."""
    try:
        return _BY_KEY[key]
    except KeyError:
        raise ValueError(
            f"Unknown V4 configuration key {key!r}. Known: {sorted(_BY_KEY)}"
        ) from None


# ---------------------------------------------------------------------------
# Position sizing per configuration (V4 activation phase, Section 8).
#
# The six-configuration evaluation decides WHICH structure a configuration
# would hold. This decides HOW MANY contracts it would hold, so that two
# configurations selecting the same frozen candidate freeze their own
# quantity, capital used and max risk while sharing one market observation.
#
# Mirrors V3's compute_budget_fit: quantity = floor(usable risk / risk per
# contract), additionally bounded by the capital base for the entry debit,
# never below one contract for an eligible candidate. No new percentage is
# introduced -- both bounds come from the configuration's existing cap and
# capital base.
# ---------------------------------------------------------------------------
from dataclasses import dataclass as _dataclass  # noqa: E402


@_dataclass(frozen=True)
class ConfigurationPosition:
    configuration_key: str
    candidate_id: str
    quantity: int
    per_contract_entry_cash: Decimal
    per_contract_max_risk: Decimal
    capital_used: Decimal
    max_risk_used: Decimal
    standardized_capital: Decimal


def size_configuration_position(
    configuration: V4Configuration,
    *,
    candidate_id: str,
    per_contract_entry_cash: Decimal,
    per_contract_max_risk: Decimal,
) -> ConfigurationPosition:
    """Quantity for an already-ELIGIBLE candidate (one contract is known to
    fit). ``per_contract_entry_cash`` is the signed executable entry cash
    for one unit (positive = debit); ``per_contract_max_risk`` its bounded
    max loss for one unit.
    """
    risk_bound = (
        int(configuration.max_risk_dollars / per_contract_max_risk)
        if per_contract_max_risk > 0
        else 1
    )
    debit = max(per_contract_entry_cash, Decimal(0))
    capital_bound = int(configuration.capital_base / debit) if debit > 0 else risk_bound
    quantity = max(1, min(risk_bound, capital_bound))
    return ConfigurationPosition(
        configuration_key=configuration.key,
        candidate_id=candidate_id,
        quantity=quantity,
        per_contract_entry_cash=per_contract_entry_cash,
        per_contract_max_risk=per_contract_max_risk,
        capital_used=debit * quantity,
        max_risk_used=per_contract_max_risk * quantity,
        standardized_capital=configuration.capital_base,
    )
