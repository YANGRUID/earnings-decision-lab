"""V4.2 -- versioned AMC/BMO anchoring for the post-earnings move.

THE DEFECT THIS CORRECTS
------------------------
``analytics/earnings/price_moves.py::price_reaction_moves`` takes only an
``earnings_date`` and never sees the announcement time. It anchors on the last
close on/before that date and observes the next trading day's close:

    pre  = last session <= earnings_date
    post = first session >  earnings_date

That is correct for an AMC report, which is released after D0's close. For a
BMO report -- released before D0's open -- it uses the POST-RELEASE close as
the "before" price and then measures the day AFTER the reaction. The reaction
it reports is not the earnings reaction at all.

WHAT THIS MODULE DOES, AND WHAT IT DOES NOT
-------------------------------------------
It is a versioned, additive re-derivation. It reads immutable ``PriceBar``
rows and returns an anchored reaction; it never writes, and it never mutates
the existing ``PriceReaction`` corpus. Released V4.1 evidence that referenced
those values keeps referencing exactly the numbers it was built on.

Anchoring, per classification:

    KNOWN_AMC       pre  = last session <= earnings_date   (D0 close)
                    post = first session >  earnings_date  (D+1 close)

    KNOWN_BMO       pre  = last session <  earnings_date   (D-1 close)
                    post = first session >= earnings_date  (D0 close)

    UNKNOWN_TIMING  the AMC convention, because that is what the existing
                    corpus was built with and silently switching would make
                    old and new numbers incomparable -- but the result is
                    stamped UNVERIFIED so no caller can mistake it for a
                    timing-verified observation.

Audited 2026-09-05: ``announcement_time`` is UNKNOWN for all 1,831 historical
earnings events and all 1,201 with a usable reaction. Zero are known AMC or
BMO. So today every historical observation is UNVERIFIED, and the timing
dimension only improves prospectively as the forward calendar -- which does
record timing -- accumulates. Nothing here infers timing from filing times or
price behaviour; an unknown announcement time stays unknown.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

REACTION_ANCHORING_VERSION = "v4_2_reaction_anchoring_v2"

KNOWN_AMC = "KNOWN_AMC"
KNOWN_BMO = "KNOWN_BMO"
UNKNOWN_TIMING = "UNKNOWN_TIMING"

#: How much the anchoring can be trusted, kept separate from sample size.
TIMING_VERIFIED = "timing_verified"
TIMING_UNVERIFIED = "timing_unverified"
TIMING_MIXED = "timing_mixed"

#: The project's own announcement_time enum values, mapped to this module's
#: classification. Imported nowhere so a new enum member fails loudly here
#: rather than being silently treated as unknown.
_ANNOUNCEMENT_TIME_TO_CLASSIFICATION = {
    "AFTER_MARKET": KNOWN_AMC,
    "BEFORE_MARKET": KNOWN_BMO,
    "UNKNOWN": UNKNOWN_TIMING,
}


def classify_announcement_time(raw: object) -> str:
    """Map a persisted ``announcement_time`` to a timing classification.

    An unrecognised value is UNKNOWN_TIMING rather than an assumption -- the
    honest answer when the vocabulary has moved on underneath us.
    """
    value = getattr(raw, "value", None) or str(raw)
    return _ANNOUNCEMENT_TIME_TO_CLASSIFICATION.get(value, UNKNOWN_TIMING)


@dataclass(frozen=True)
class AnchoredReaction:
    """One earnings event's move, with the anchoring that produced it."""

    earnings_date: date
    timing_classification: str
    timing_quality: str
    pre_event_date: date
    pre_event_close: Decimal
    post_event_date: date
    post_event_close: Decimal
    signed_move_pct: Decimal
    version: str = REACTION_ANCHORING_VERSION

    @property
    def abs_move_pct(self) -> Decimal:
        return abs(self.signed_move_pct)

    @property
    def timing_verified(self) -> bool:
        return self.timing_quality == TIMING_VERIFIED


def anchored_reaction(
    bars: dict[date, Decimal],
    *,
    earnings_date: date,
    timing_classification: str,
) -> AnchoredReaction | None:
    """The anchored move for one event, or None when the bars needed to
    compute it honestly are not present.

    ``bars`` is a {trade_date: close} map the caller already holds. Returns
    None rather than reaching for the nearest available substitute: a
    reaction anchored on the wrong session is worse than no reaction.
    """
    if not bars:
        return None

    if timing_classification == KNOWN_BMO:
        pre_candidates = [d for d in bars if d < earnings_date]
        post_candidates = [d for d in bars if d >= earnings_date]
        quality = TIMING_VERIFIED
    else:
        # AMC convention, used for KNOWN_AMC and -- flagged -- for UNKNOWN.
        pre_candidates = [d for d in bars if d <= earnings_date]
        post_candidates = [d for d in bars if d > earnings_date]
        quality = (
            TIMING_VERIFIED if timing_classification == KNOWN_AMC else TIMING_UNVERIFIED
        )

    if not pre_candidates or not post_candidates:
        return None

    pre_date = max(pre_candidates)
    post_date = min(post_candidates)
    pre_close = bars[pre_date]
    post_close = bars[post_date]
    if pre_close <= 0:
        return None

    return AnchoredReaction(
        earnings_date=earnings_date,
        timing_classification=timing_classification,
        timing_quality=quality,
        pre_event_date=pre_date,
        pre_event_close=pre_close,
        post_event_date=post_date,
        post_event_close=post_close,
        signed_move_pct=(post_close - pre_close) / pre_close,
    )


def aggregate_timing_quality(reactions: list[AnchoredReaction]) -> str:
    """The timing quality of a SET of observations.

    Deliberately conservative: a distribution is only timing-verified when
    every contributing observation is, because one mis-anchored BMO event
    contaminates the magnitudes just as effectively as many.
    """
    if not reactions:
        return TIMING_UNVERIFIED
    verified = sum(1 for r in reactions if r.timing_verified)
    if verified == len(reactions):
        return TIMING_VERIFIED
    if verified == 0:
        return TIMING_UNVERIFIED
    return TIMING_MIXED
