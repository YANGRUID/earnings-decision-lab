"""V4.2 CHALLENGER -- an earnings-specific execution-friction cohort.

NOT ACTIVE. V4.4A's LOW/NORMAL/HIGH = 4%/10%/18% remain the production
friction model and are untouched by this module.

The audit's finding was about SOURCING, not about the numbers being wrong for
what they describe. Those quantiles are real p25/p50/p75 relative spreads --
but computed over ``options_snapshot``, a general options universe of ~700
rows. The options V4 actually trades are short-dated contracts around an
earnings event, and the seven events observed so far have a materially fatter
right tail: 2 of 7 selected candidates carried a mean relative spread above
the modeled HIGH, and 4 of 7 had at least one leg above it, the worst at 50%.

That is a reason to rebuild the cohort from a comparable universe. It is NOT
a reason to refit the constants to seven outcomes, and nothing here does: the
inputs are spreads observed at entry, which are market-microstructure facts
independent of how any position turned out, and the cohort refuses to produce
a model at all until it holds a sample comparable in size to the one it would
replace.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

EARNINGS_FRICTION_VERSION = "earnings_friction_v2"

#: The incumbent model was built from ~700 real two-sided rows. A replacement
#: claiming to describe a different universe should not be promoted on less
#: evidence than the thing it replaces, so the cohort stays advisory until it
#: holds a comparable sample across a meaningful number of distinct events.
#: Neither number is fitted to an outcome; both are statements about how much
#: evidence a distribution needs before it is worth trusting.
MIN_OBSERVATIONS_FOR_MODEL = 700
MIN_EVENTS_FOR_MODEL = 30

STATUS_ADVISORY = "ADVISORY_INSUFFICIENT_SAMPLE"
STATUS_READY = "READY_FOR_REVIEW"


@dataclass(frozen=True)
class FrictionObservation:
    """One real, two-sided leg quote observed at a decision instant.

    ``volume`` and ``open_interest`` are optional because the V4 shadow
    persistence does not currently write them (audited 2026-09-05: 0 of 211
    persisted legs carry either, although the provider does request the
    generic ticks that supply them). They are accepted here so the cohort
    gains those dimensions automatically once that gap is closed.
    """

    relative_spread: Decimal
    absolute_spread: Decimal
    dte: int
    moneyness: Decimal | None
    right: str | None = None
    expiration: date | None = None
    market_data_quality: str | None = None
    volume: int | None = None
    open_interest: int | None = None


def _quantile(ordered: list[Decimal], q: Decimal) -> Decimal:
    if len(ordered) == 1:
        return ordered[0]
    position = (Decimal(len(ordered)) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


@dataclass(frozen=True)
class EarningsFrictionCohort:
    """The accumulating earnings-option spread distribution, with an explicit
    statement of whether it is yet allowed to become a model."""

    observations: int
    distinct_events: int
    status: str
    version: str = EARNINGS_FRICTION_VERSION
    p25_relative_spread: Decimal | None = None
    p50_relative_spread: Decimal | None = None
    p75_relative_spread: Decimal | None = None
    p90_relative_spread: Decimal | None = None
    max_relative_spread: Decimal | None = None
    median_dte: int | None = None

    @property
    def ready(self) -> bool:
        return self.status == STATUS_READY

    def proposed_friction_levels(self) -> dict[str, Decimal] | None:
        """The LOW/NORMAL/HIGH triple this cohort would imply, using the SAME
        p25/p50/p75 construction as the incumbent so the two are comparable.

        Returns None while the sample is advisory -- a friction model built on
        thin evidence would be a worse error than the sourcing mismatch it is
        meant to correct.
        """
        if not self.ready or self.p25_relative_spread is None:
            return None
        return {
            "LOW_FRICTION": self.p25_relative_spread,
            "NORMAL_FRICTION": self.p50_relative_spread,  # type: ignore[dict-item]
            "HIGH_FRICTION": self.p75_relative_spread,  # type: ignore[dict-item]
        }


def build_earnings_friction_cohort(
    observations: list[FrictionObservation], *, distinct_events: int
) -> EarningsFrictionCohort:
    """Pure aggregation over already-observed entry spreads."""
    n = len(observations)
    status = (
        STATUS_READY
        if n >= MIN_OBSERVATIONS_FOR_MODEL and distinct_events >= MIN_EVENTS_FOR_MODEL
        else STATUS_ADVISORY
    )
    if n == 0:
        return EarningsFrictionCohort(
            observations=0, distinct_events=distinct_events, status=status
        )

    spreads = sorted(o.relative_spread for o in observations)
    dtes = sorted(o.dte for o in observations)
    return EarningsFrictionCohort(
        observations=n,
        distinct_events=distinct_events,
        status=status,
        p25_relative_spread=_quantile(spreads, Decimal("0.25")),
        p50_relative_spread=_quantile(spreads, Decimal("0.50")),
        p75_relative_spread=_quantile(spreads, Decimal("0.75")),
        p90_relative_spread=_quantile(spreads, Decimal("0.90")) if n >= 10 else None,
        max_relative_spread=spreads[-1],
        median_dte=dtes[len(dtes) // 2],
    )
