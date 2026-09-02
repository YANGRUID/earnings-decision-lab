"""Read-only Operations diagnostic view over QuoteAcquisitionAttempt rows
(Phase 4 quote-observability hardening, 2026-08-26, Sections 13-14) --
turns raw per-(leg, poll) telemetry into the per-leg summary an Operator
reads directly, plus a bounded cross-capture aggregate.

Never exposes an account id, username, session id, cookie, auth token,
or password -- QuoteAcquisitionAttempt itself never stores any (see its
own model docstring); this module only re-shapes what's already there.
Pure read, no side effects, no live provider call: every value is either
a real, already-persisted QuoteAcquisitionAttempt row or a computation
over a batch of them.

Deliberately never called "performance" or "win rate" anywhere in this
module -- Section 14 -- these are diagnostic statistics about the quote-
acquisition PROCESS (how many polls it took, how often a provider
exception interrupted it), never a claim about trading outcomes.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import mean, median

from sqlalchemy.orm import Session

from models.entry_capture_attempt import EntryCaptureAttempt
from models.enums import CaptureStatus
from models.quote_acquisition_attempt import QuoteAcquisitionAttempt
from models.settlement_capture_attempt import SettlementCaptureAttempt
from services.entry_failure_taxonomy import requirement_met

# Section 19 -- the one stable, positive signal that a capture attempt
# actually reached the real provider call (get_quotes_for_selected_legs/
# get_quotes_for_known_contracts + get_underlying_quote), regardless of
# which specific exception was raised: both benchmark_entry_capture.py's
# and benchmark_exit_capture.py's own except blocks build capture_error
# with this exact prefix. Deliberately NOT keyed on the early-return
# validation messages ("no recommended strategy legs", "no selected_
# expiration", "no legs on record to close", window/eligibility reasons)
# -- those are a real, growing, driftable set; this one prefix is not.
_PROVIDER_CALL_FAILURE_PREFIX = "options provider call failed:"

# Bounded lookback for the alert scan (Section 19), matching this
# project's other Operations aggregates.
MISSING_TELEMETRY_LOOKBACK = timedelta(hours=72)

# Post-official-run validation (2026-08-27), Section 20 -- the real,
# permanent instant before which QuoteAcquisitionAttempt could not have
# existed: this table was created by migration 00b397af8ecb ("add
# quote_acquisition_attempt table"), deployed 2026-08-26 17:15:12 UTC.
# A capture attempt created before this instant (e.g. DY's Aug 25 entry)
# can never acquire telemetry without violating this project's own
# immutability guarantee -- it is not a wiring gap, it is a structural
# fact about when the feature came into existence, so it must never
# render as an active Operations alert, not even for the first 72h.
TELEMETRY_FEATURE_ACTIVATED_AT = datetime(2026, 8, 26, 17, 15, 12, tzinfo=UTC)

# Bounded window for the aggregate summary (Section 14) -- matches this
# project's other Operations aggregates (e.g. services/operations.py's
# own FAILURE_LOOKBACK), never an unbounded full-table scan.
DIAGNOSTICS_SUMMARY_LOOKBACK = timedelta(hours=48)


@dataclass(frozen=True)
class QuoteDiagnosticAttempt:
    snapshot_attempt_number: int
    elapsed_ms: int
    bid: Decimal | None
    ask: Decimal | None
    last_price: Decimal | None
    bid_present: bool
    ask_present: bool
    last_present: bool
    market_data_quality: str | None


@dataclass(frozen=True)
class QuoteDiagnosticLeg:
    leg_index: int | None
    option_type: str | None
    strike: Decimal | None
    required_side: str
    contract_resolved: bool
    external_contract_id: str | None
    attempts: list[QuoteDiagnosticAttempt]
    result_label: str


@dataclass(frozen=True)
class QuoteDiagnostics:
    ticker: str
    expiration: str | None
    legs: list[QuoteDiagnosticLeg]


@dataclass(frozen=True)
class QuoteDiagnosticsSummary:
    window_hours: int
    contracts_requested: int
    contracts_resolved: int
    total_snapshot_attempts: int
    average_attempts_per_leg: float | None
    median_attempts_per_leg: float | None
    quote_unavailable_count: int
    rate_limited_count: int
    permission_error_count: int
    contract_error_count: int


def _result_label(rows_for_leg: list[QuoteAcquisitionAttempt]) -> str:
    final = next((r for r in rows_for_leg if r.final_for_leg), rows_for_leg[-1])
    if final.rate_limited:
        return "RATE LIMITED"
    if final.permission_error:
        return "PERMISSION ERROR"
    if not final.contract_resolved:
        return "CONTRACT UNAVAILABLE"
    side_label = final.required_side.value.upper()
    if requirement_met(final):
        elapsed_s = final.elapsed_ms / 1000
        return f"{side_label} acquired after {elapsed_s:.3f}s"
    return f"{side_label} unavailable after bounded retry"


def _to_diagnostics(rows: list[QuoteAcquisitionAttempt]) -> QuoteDiagnostics | None:
    if not rows:
        return None
    rows_by_leg: dict[int | None, list[QuoteAcquisitionAttempt]] = {}
    leg_order: list[int | None] = []
    for row in rows:
        key = row.leg_index
        if key not in rows_by_leg:
            rows_by_leg[key] = []
            leg_order.append(key)
        rows_by_leg[key].append(row)

    legs: list[QuoteDiagnosticLeg] = []
    for key in leg_order:
        leg_rows = sorted(rows_by_leg[key], key=lambda r: r.snapshot_attempt_number)
        first = leg_rows[0]
        legs.append(
            QuoteDiagnosticLeg(
                leg_index=first.leg_index,
                option_type=first.option_type.value if first.option_type else None,
                strike=first.strike,
                required_side=first.required_side.value,
                contract_resolved=any(r.contract_resolved for r in leg_rows),
                external_contract_id=next(
                    (r.external_contract_id for r in leg_rows if r.external_contract_id), None
                ),
                attempts=[
                    QuoteDiagnosticAttempt(
                        snapshot_attempt_number=r.snapshot_attempt_number,
                        elapsed_ms=r.elapsed_ms,
                        bid=r.bid,
                        ask=r.ask,
                        last_price=r.last_price,
                        bid_present=r.bid_present,
                        ask_present=r.ask_present,
                        last_present=r.last_present,
                        market_data_quality=r.market_data_quality,
                    )
                    for r in leg_rows
                ],
                result_label=_result_label(leg_rows),
            )
        )

    return QuoteDiagnostics(ticker=rows[0].ticker, expiration=_expiration_label(rows), legs=legs)


def _expiration_label(rows: list[QuoteAcquisitionAttempt]) -> str | None:
    expiration = next((r.expiration for r in rows if r.expiration is not None), None)
    return expiration.isoformat() if expiration is not None else None


def get_entry_quote_diagnostics(
    db: Session, entry_capture_attempt_id: int
) -> QuoteDiagnostics | None:
    """``None`` when no telemetry exists for this attempt -- a legacy
    (Aug 25) capture, or a real capture whose own writer failed before
    persisting anything at all (never fabricated as an empty-but-present
    result)."""
    rows = (
        db.query(QuoteAcquisitionAttempt)
        .filter_by(entry_capture_attempt_id=entry_capture_attempt_id)
        .order_by(
            QuoteAcquisitionAttempt.leg_index, QuoteAcquisitionAttempt.snapshot_attempt_number
        )
        .all()
    )
    return _to_diagnostics(rows)


def get_settlement_quote_diagnostics(
    db: Session, settlement_capture_attempt_id: int
) -> QuoteDiagnostics | None:
    rows = (
        db.query(QuoteAcquisitionAttempt)
        .filter_by(settlement_capture_attempt_id=settlement_capture_attempt_id)
        .order_by(
            QuoteAcquisitionAttempt.leg_index, QuoteAcquisitionAttempt.snapshot_attempt_number
        )
        .all()
    )
    return _to_diagnostics(rows)


def get_quote_diagnostics_summary(
    db: Session, *, now: datetime | None = None, window_hours: int = 48
) -> QuoteDiagnosticsSummary:
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=window_hours)
    rows = (
        db.query(QuoteAcquisitionAttempt).filter(QuoteAcquisitionAttempt.observed_at >= since).all()
    )

    final_rows = [r for r in rows if r.final_for_leg]
    attempts_per_leg: dict[tuple, int] = {}
    for row in rows:
        key = (
            row.capture_attempt_type,
            row.entry_capture_attempt_id,
            row.settlement_capture_attempt_id,
            row.leg_index,
        )
        attempts_per_leg[key] = max(attempts_per_leg.get(key, 0), row.snapshot_attempt_number)

    counts = list(attempts_per_leg.values())

    return QuoteDiagnosticsSummary(
        window_hours=window_hours,
        contracts_requested=len(final_rows),
        contracts_resolved=sum(1 for r in final_rows if r.contract_resolved),
        total_snapshot_attempts=len(rows),
        average_attempts_per_leg=round(mean(counts), 2) if counts else None,
        median_attempts_per_leg=round(median(counts), 2) if counts else None,
        quote_unavailable_count=sum(
            1
            for r in final_rows
            if r.contract_resolved
            and not requirement_met(r)
            and not r.rate_limited
            and not r.permission_error
        ),
        rate_limited_count=sum(1 for r in final_rows if r.rate_limited),
        permission_error_count=sum(1 for r in final_rows if r.permission_error),
        contract_error_count=sum(1 for r in final_rows if not r.contract_resolved),
    )


@dataclass(frozen=True)
class MissingTelemetryAlert:
    """Section 19 -- a real capture attempt that reached the provider
    call (its own capture_error carries the one stable ``options
    provider call failed:`` prefix both benchmark_entry_capture.py and
    benchmark_exit_capture.py use, or it CAPTURED outright) but has zero
    QuoteAcquisitionAttempt rows -- validates the telemetry wiring
    prospectively, on the next real official capture, rather than
    trusting it silently. Deliberately excludes every early-return
    validation failure (no legs, no expiration, window/eligibility) --
    those never call the provider at all, so having no telemetry there
    is correct, not a gap."""

    capture_attempt_type: str
    entry_capture_attempt_id: int | None
    settlement_capture_attempt_id: int | None
    ticker: str
    occurred_at: datetime
    capture_error: str | None


def detect_missing_quote_telemetry(
    db: Session, *, now: datetime | None = None
) -> list[MissingTelemetryAlert]:
    now = now or datetime.now(UTC)
    # A capture attempt from before the telemetry feature existed is
    # never a candidate, regardless of how recent the rolling lookback
    # window is -- see TELEMETRY_FEATURE_ACTIVATED_AT above.
    since = max(now - MISSING_TELEMETRY_LOOKBACK, TELEMETRY_FEATURE_ACTIVATED_AT)

    telemetered_entry_ids = {
        row[0]
        for row in db.query(QuoteAcquisitionAttempt.entry_capture_attempt_id)
        .filter(QuoteAcquisitionAttempt.entry_capture_attempt_id.isnot(None))
        .distinct()
    }
    telemetered_settlement_ids = {
        row[0]
        for row in db.query(QuoteAcquisitionAttempt.settlement_capture_attempt_id)
        .filter(QuoteAcquisitionAttempt.settlement_capture_attempt_id.isnot(None))
        .distinct()
    }

    alerts: list[MissingTelemetryAlert] = []
    entry_candidates = (
        db.query(EntryCaptureAttempt)
        .filter(EntryCaptureAttempt.created_at >= since)
        .filter(
            (EntryCaptureAttempt.status == CaptureStatus.CAPTURED)
            | (EntryCaptureAttempt.capture_error.like(f"{_PROVIDER_CALL_FAILURE_PREFIX}%"))
        )
        .all()
    )
    for attempt in entry_candidates:
        if attempt.id not in telemetered_entry_ids:
            alerts.append(
                MissingTelemetryAlert(
                    capture_attempt_type="entry",
                    entry_capture_attempt_id=attempt.id,
                    settlement_capture_attempt_id=None,
                    ticker=attempt.decision_snapshot.ticker,
                    occurred_at=attempt.created_at,
                    capture_error=attempt.capture_error,
                )
            )

    settlement_candidates = (
        db.query(SettlementCaptureAttempt)
        .filter(SettlementCaptureAttempt.created_at >= since)
        .filter(
            (SettlementCaptureAttempt.status == CaptureStatus.CAPTURED)
            | (SettlementCaptureAttempt.capture_error.like(f"{_PROVIDER_CALL_FAILURE_PREFIX}%"))
        )
        .all()
    )
    for settlement_attempt in settlement_candidates:
        if settlement_attempt.id not in telemetered_settlement_ids:
            alerts.append(
                MissingTelemetryAlert(
                    capture_attempt_type="settlement",
                    entry_capture_attempt_id=None,
                    settlement_capture_attempt_id=settlement_attempt.id,
                    ticker=settlement_attempt.decision_snapshot.ticker,
                    occurred_at=settlement_attempt.created_at,
                    capture_error=settlement_attempt.capture_error,
                )
            )

    return alerts
