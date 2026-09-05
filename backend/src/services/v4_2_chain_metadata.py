"""Freeze the listed option metadata that existed at a decision instant.

The seven historical V4 events froze exactly one expiration each, which is
why their multi-expiry behaviour CANNOT_REPLAY_HONESTLY: the chain as it
stood at that moment is simply gone, and today's chain is a different object.
Freezing this prospectively is what makes the question answerable for every
future event -- which expirations existed, which strikes were listed, and why
the bounded ladder considered the three it did.

Metadata only. One security-definition request, no contract resolution and no
market-data subscription, so this is affordable on every event and can never
turn into a chain-wide quote sweep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.decision.v4_2_expiry_ladder import (
    DEFAULT_MAX_VARIANTS,
    EXPIRY_LADDER_VERSION,
    build_expiry_ladder,
)
from models.v4_2_challenger import V4ChainMetadataSnapshot

METADATA_QUALITY_COMPLETE = "listed_metadata_complete"
METADATA_QUALITY_UNAVAILABLE = "listed_metadata_unavailable"


@dataclass
class ChainMetadataCapture:
    ticker: str
    frozen: bool
    snapshot_id: int | None = None
    expirations: list[date] | None = None
    considered: list[dict] | None = None
    strike_count: int = 0
    metadata_requests: int = 0
    market_data_requests: int = 0
    reason: str | None = None


def capture_chain_metadata(
    db: Session,
    *,
    provider,
    ticker: str,
    earnings_calendar_event_id: int,
    earnings_date: date,
    settlement_date: date,
    decision_date: date,
    observed_at: datetime,
    max_variants: int = DEFAULT_MAX_VARIANTS,
    dry_run: bool = True,
) -> ChainMetadataCapture:
    """Read the listed metadata and, unless this is a dry run, freeze it.

    Idempotent on (event, observation window): a second capture for the same
    instant returns the existing snapshot rather than writing a rival one.
    """
    existing = (
        db.query(V4ChainMetadataSnapshot)
        .filter_by(
            earnings_calendar_event_id=earnings_calendar_event_id, observed_at=observed_at
        )
        .one_or_none()
    )
    if existing is not None:
        return ChainMetadataCapture(
            ticker=ticker,
            frozen=False,
            snapshot_id=existing.id,
            reason="already frozen for this observation window",
        )

    metadata = provider.get_chain_metadata(ticker)
    if metadata is None:
        return ChainMetadataCapture(
            ticker=ticker,
            frozen=False,
            metadata_requests=1,
            reason="provider returned no listed option metadata for this underlying",
        )

    expirations: list[date] = list(metadata.get("expirations") or [])
    strikes: list[Decimal] = list(metadata.get("strikes") or [])
    ladder = build_expiry_ladder(
        set(expirations),
        earnings_date=earnings_date,
        settlement_date=settlement_date,
        decision_date=decision_date,
        max_variants=max_variants,
    )
    considered = [
        {
            "expiration": variant.expiration.isoformat(),
            "ladder_position": variant.ladder_position,
            "entry_dte": variant.entry_dte,
            "dte_at_settlement": variant.dte_at_settlement,
            "settlement_risk": variant.settlement_risk,
            "expires_on_settlement_date": variant.expires_on_settlement_date,
        }
        for variant in ladder
    ]

    capture = ChainMetadataCapture(
        ticker=ticker,
        frozen=False,
        expirations=expirations,
        considered=considered,
        strike_count=len(strikes),
        metadata_requests=1,
        market_data_requests=0,
    )
    if dry_run:
        capture.reason = "dry run: nothing written"
        return capture

    snapshot = V4ChainMetadataSnapshot(
        earnings_calendar_event_id=earnings_calendar_event_id,
        ticker=ticker,
        observed_at=observed_at,
        underlying_conid=metadata.get("underlying_conid"),
        trading_class=metadata.get("trading_class"),
        exchange=metadata.get("exchange"),
        multiplier=str(metadata.get("multiplier")) if metadata.get("multiplier") else None,
        available_expirations=[d.isoformat() for d in expirations],
        # Strikes are chain-wide in IBKR's security definition, so they are
        # recorded once for the chain rather than duplicated per expiry --
        # and recording them implies nothing about any of them being quoted.
        listed_strikes={"chain": [str(k) for k in strikes]},
        considered_expirations=considered,
        source_provider=metadata.get("source_provider"),
        metadata_quality=METADATA_QUALITY_COMPLETE,
        expiry_ladder_version=EXPIRY_LADDER_VERSION,
    )
    db.add(snapshot)
    db.flush()
    capture.frozen = True
    capture.snapshot_id = snapshot.id
    return capture
