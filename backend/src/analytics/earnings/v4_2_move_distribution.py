"""V4.2 CHALLENGER -- the point-in-time historical post-earnings move
distribution a move-edge test can actually be computed from.

Deliberately NOT a third competing move formula. The observation itself is
``PriceReaction.next_day_move_pct`` -- the same value the rest of the
product already uses -- and the sample-size tiers are the project's own
``MIN_N_FOR_MEDIAN`` / ``QUARTILES`` / ``DECILES`` constants, imported rather
than restated. What this module adds is what V4.2 needs and V4.1 does not
have: signed statistics, exceedance against a market-implied level, explicit
timing provenance, and a distribution object that carries its own sample
size and quality so no caller can silently treat thin evidence as strong.

TIMING PROVENANCE, STATED PLAINLY
---------------------------------
V4's live objective is entry at 15:30 ET on the pre-earnings session and
settlement at 15:30 ET on the first post-earnings session. The historical
analog available in this database is close-to-close: ``price_reaction_moves``
anchors on the last close on/before the earnings date and observes the next
trading day's close.

Two honest caveats travel with every distribution built here, and both are
recorded on the object rather than left to a reader's assumption:

  * the two ends differ from the live objective by the 15:30-16:00 window on
    each side (``CLOSE_TO_CLOSE_D0_D1``); and
  * ``earnings_event.announcement_time`` is ``UNKNOWN`` for essentially the
    entire historical corpus, so the anchoring is correct for an AMC event
    and shifted by one session for a BMO one. Audited 2026-09-05: every
    historical row for the seven V4 tickers carries UNKNOWN, and the forward
    calendar (which does record timing) only reaches back to 2026-08-25, so
    the timing cannot be recovered for these events.

The second caveat adds noise to the magnitude distribution; it is not a
directional bias. It is reported, never silently absorbed.
"""

from dataclasses import dataclass
from decimal import Decimal
from statistics import median as _median

from analytics.decision.v4_expected_move import (
    MIN_N_FOR_DECILES,
    MIN_N_FOR_MEDIAN,
    MIN_N_FOR_QUARTILES,
)

MOVE_DISTRIBUTION_VERSION = "v4_2_move_distribution_v1"

#: How the historical observation was anchored, carried on every result.
TIMING_CLOSE_TO_CLOSE = "CLOSE_TO_CLOSE_D0_D1"
#: Whether the AMC/BMO timing behind that anchoring was actually known.
TIMING_VERIFIED = "ANNOUNCEMENT_TIME_VERIFIED"
TIMING_UNVERIFIED = "ANNOUNCEMENT_TIME_UNKNOWN_ASSUMED_AMC_ANCHORING"

QUALITY_INSUFFICIENT = "insufficient"
QUALITY_LIMITED = "limited"
QUALITY_QUARTILES = "adequate_quartiles"
QUALITY_DECILES = "adequate_deciles"


def evidence_quality(n: int) -> str:
    """The project's existing tiers, applied to this sample. Kept as one
    function so a caller can never disagree with v4_expected_move about what
    counts as thin evidence."""
    if n < MIN_N_FOR_MEDIAN:
        return QUALITY_INSUFFICIENT
    if n < MIN_N_FOR_QUARTILES:
        return QUALITY_LIMITED
    if n < MIN_N_FOR_DECILES:
        return QUALITY_QUARTILES
    return QUALITY_DECILES


def _quantile(ordered: list[Decimal], q: Decimal) -> Decimal:
    """Linear interpolation between order statistics, matching
    v4_expected_move's own convention."""
    if len(ordered) == 1:
        return ordered[0]
    position = (Decimal(len(ordered)) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return ordered[low] + (ordered[high] - ordered[low]) * weight


@dataclass(frozen=True)
class MoveDistribution:
    """One company's post-earnings move distribution as of a point in time.

    Every quantile is None below the sample size that supports it -- a p90
    from four observations is false precision, not information.
    """

    sample_n: int
    quality: str
    as_of: object  # the exclusive point-in-time boundary (date)
    timing_method: str
    timing_provenance: str
    version: str = MOVE_DISTRIBUTION_VERSION

    median_abs_move_pct: Decimal | None = None
    mean_abs_move_pct: Decimal | None = None
    p25_abs_move_pct: Decimal | None = None
    p75_abs_move_pct: Decimal | None = None
    p10_abs_move_pct: Decimal | None = None
    p90_abs_move_pct: Decimal | None = None
    up_frequency: Decimal | None = None
    down_frequency: Decimal | None = None
    abs_moves: tuple[Decimal, ...] = ()
    signed_moves: tuple[Decimal, ...] = ()

    @property
    def usable(self) -> bool:
        """Whether a central tendency can be quoted at all."""
        return self.median_abs_move_pct is not None

    def exceedance_frequency(self, level: Decimal) -> Decimal | None:
        """Observed fraction of past moves whose MAGNITUDE exceeded ``level``.

        Reported as a supporting diagnostic rather than the primary edge
        statistic: at the sample sizes available here (10-48) a proportion
        carries a standard error of roughly 7-15 percentage points, while the
        median is far more stable. Using it as the gate would put more weight
        on the tail than the evidence can bear.
        """
        if not self.abs_moves or level is None or level <= 0:
            return None
        hits = sum(1 for m in self.abs_moves if m > level)
        return Decimal(hits) / Decimal(len(self.abs_moves))


def build_move_distribution(
    signed_moves: list[Decimal],
    *,
    as_of: object,
    timing_method: str = TIMING_CLOSE_TO_CLOSE,
    timing_provenance: str = TIMING_UNVERIFIED,
) -> MoveDistribution:
    """Pure. ``signed_moves`` must already be point-in-time filtered by the
    caller -- this function has no way to check that and never assumes it."""
    n = len(signed_moves)
    if n == 0:
        return MoveDistribution(
            sample_n=0,
            quality=QUALITY_INSUFFICIENT,
            as_of=as_of,
            timing_method=timing_method,
            timing_provenance=timing_provenance,
        )

    abs_moves = [abs(m) for m in signed_moves]
    ordered = sorted(abs_moves)
    ups = sum(1 for m in signed_moves if m > 0)
    downs = sum(1 for m in signed_moves if m < 0)

    quartiles_ok = n >= MIN_N_FOR_QUARTILES
    deciles_ok = n >= MIN_N_FOR_DECILES

    return MoveDistribution(
        sample_n=n,
        quality=evidence_quality(n),
        as_of=as_of,
        timing_method=timing_method,
        timing_provenance=timing_provenance,
        median_abs_move_pct=_median(abs_moves),
        mean_abs_move_pct=sum(abs_moves, Decimal(0)) / n,
        p25_abs_move_pct=_quantile(ordered, Decimal("0.25")) if quartiles_ok else None,
        p75_abs_move_pct=_quantile(ordered, Decimal("0.75")) if quartiles_ok else None,
        p10_abs_move_pct=_quantile(ordered, Decimal("0.10")) if deciles_ok else None,
        p90_abs_move_pct=_quantile(ordered, Decimal("0.90")) if deciles_ok else None,
        up_frequency=Decimal(ups) / Decimal(n),
        down_frequency=Decimal(downs) / Decimal(n),
        abs_moves=tuple(abs_moves),
        signed_moves=tuple(signed_moves),
    )
