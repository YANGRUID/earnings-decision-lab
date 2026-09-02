"""Phase 4 market-data-quality hardening (2026-08-26), Sections 16-17 --
the official benchmark's explicit LIVE/DELAYED data policy, and the
derived, honest quality label a capture's real per-leg/underlying
quality values earn. Pure functions, no DB/network access: every input
is a real, already-observed ``market_data_quality`` string (live/
delayed/frozen/unknown/unavailable, see providers/ibkr_client.py's
decode_market_data_quality) a caller already has in hand.
"""

from typing import Literal

from models.enums import MarketDataQualityPolicy

CaptureQualityLabel = Literal["VERIFIED_LIVE", "DELAYED_DATA", "UNKNOWN_QUALITY"]


def enforce_market_data_quality_policy(
    policy: MarketDataQualityPolicy, quality_values: list[str | None]
) -> str | None:
    """Returns a real, honest failure reason when ``policy`` rejects the
    observed quality set, else ``None`` (accepted). Under the default
    ``ALLOW_DELAYED_WITH_LABEL``, every quality value is accepted --
    this is a no-op today, matching this project's actual, already-real
    capture behavior (see MarketDataQualityPolicy's own docstring for
    why). Under ``LIVE_ONLY``, any leg/underlying quote whose quality
    isn't exactly ``"live"`` fails the whole capture -- never partially
    accepted, since a mixed live/delayed fill would misrepresent the
    entire position's provenance.
    """
    if policy != MarketDataQualityPolicy.LIVE_ONLY:
        return None
    non_live = sorted({q or "unknown" for q in quality_values if q != "live"})
    if not non_live:
        return None
    return (
        "official policy requires LIVE market data (market_data_quality_policy="
        f"live_only) -- observed non-live quality: {non_live}"
    )


def derive_capture_quality_label(quality_values: list[str | None]) -> CaptureQualityLabel:
    """Section 17 -- a single, honest label for an entire capture (every
    leg's quote quality plus the underlying's), never invisibly combined
    with a differently-sourced capture. ``VERIFIED_LIVE`` only when every
    real value present is exactly "live"; ``UNKNOWN_QUALITY`` when any
    value is missing or genuinely unrecognized (never assumed delayed);
    ``DELAYED_DATA`` covers delayed/frozen/unavailable -- real, known,
    just not live.
    """
    if not quality_values or any(q is None for q in quality_values):
        return "UNKNOWN_QUALITY"
    if all(q == "live" for q in quality_values):
        return "VERIFIED_LIVE"
    known = {"live", "delayed", "frozen", "unavailable"}
    if all(q in known for q in quality_values):
        return "DELAYED_DATA"
    return "UNKNOWN_QUALITY"
