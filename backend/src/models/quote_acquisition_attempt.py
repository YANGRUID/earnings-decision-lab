"""Persistent, per-poll quote-acquisition telemetry (IBKR execution-
observability hardening, 2026-08-26) -- real diagnostic evidence for a
FUTURE official EntryCaptureAttempt or SettlementCaptureAttempt, so a
scheduler-run failure is diagnosable from Operations without terminal or
Docker-log archaeology.

Deliberately a separate, operational/diagnostic child table, never a
widening of EntryCaptureAttempt/EntrySnapshot/SettlementCaptureAttempt/
ExitSnapshot themselves -- those remain exactly the official trade
evidence they already are (Phase 4's own immutable "point-in-time
decision, entry, and settlement" record). A row here is never read by
any pricing or settlement logic; the official fill still comes only from
EntrySnapshot.benchmark_entry_price / ExitSnapshot.benchmark_exit_price,
computed exactly as before (see services/benchmark_entry_capture.py's
own _price_leg, services/benchmark_exit_capture.py's own
_price_exit_leg) -- this table is a real, honest OBSERVATION of what a
poll saw, never itself a source of truth for a trade.

Insert-only by convention: each poll attempt is a distinct, honest,
point-in-time observation, and an old observation is never updated to
look cleaner later. Deliberately NOT given the same hard Postgres
BEFORE UPDATE trigger the official Phase 4 snapshot tables use --
those protect immutable financial evidence a regulator-grade audit
trail depends on; this table protects a real but lower-stakes
diagnostic invariant, and the append-only rule is enforced by never
writing UPDATE code against it, not by a DB-level guarantee.

Never stores an account id, username, session id, cookie, auth token,
or password -- ``external_contract_id`` is IBKR's own public per-
contract identifier (already stored, unredacted, on EntrySnapshot/
ExitSnapshot), not a credential.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, Enum, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base
from models.enums import OptionType, QuoteAcquisitionCaptureType, QuoteRequirement

NUM = Numeric(18, 6)


class QuoteAcquisitionAttempt(Base):
    """Grain: one row per real (capture attempt, leg/contract, snapshot
    poll) -- attempt 0 (see ``contract_resolved``) records a leg whose
    exact contract could not even be resolved; attempts 1..N record each
    real ``/iserver/marketdata/snapshot`` poll after that."""

    __tablename__ = "quote_acquisition_attempt"

    id: Mapped[int] = mapped_column(primary_key=True)

    capture_attempt_type: Mapped[QuoteAcquisitionCaptureType] = mapped_column(
        Enum(QuoteAcquisitionCaptureType, name="quote_acquisition_capture_type")
    )
    # Exactly one of these two is set, matching capture_attempt_type --
    # never enforced by a DB CHECK constraint (a diagnostic table, not
    # trade evidence), but always true by construction (see
    # services/quote_telemetry.py, the one real writer).
    entry_capture_attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("entry_capture_attempt.id"), index=True
    )
    settlement_capture_attempt_id: Mapped[int | None] = mapped_column(
        ForeignKey("settlement_capture_attempt.id"), index=True
    )

    ticker: Mapped[str] = mapped_column(String(16), index=True)
    # 0-indexed position within the real leg list this attempt belongs
    # to (decision_snapshot.legs for entry, entry_snapshot rows for
    # settlement) -- best-effort: matched by (strike, option_type)
    # against that real list, never fabricated when no match is found.
    leg_index: Mapped[int | None] = mapped_column(Integer)
    expiration: Mapped[date | None] = mapped_column(Date)
    option_type: Mapped[OptionType | None] = mapped_column(
        Enum(OptionType, name="option_type", create_type=False)
    )
    strike: Mapped[Decimal | None] = mapped_column(NUM)
    # IBKR's own public per-contract identifier -- not a secret; already
    # stored unredacted on EntrySnapshot/ExitSnapshot.
    external_contract_id: Mapped[str | None] = mapped_column(String(32))

    required_side: Mapped[QuoteRequirement] = mapped_column(
        Enum(QuoteRequirement, name="quote_requirement")
    )

    # 0 = contract-resolution attempt (see contract_resolved below); a
    # real poll attempt otherwise, 1-indexed, matching
    # SnapshotAttempt.attempt from providers/ibkr_options.py.
    snapshot_attempt_number: Mapped[int] = mapped_column(Integer)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Milliseconds since the real priming call returned -- always >= 0;
    # 0 for the contract-resolution row, which precedes any poll.
    elapsed_ms: Mapped[int] = mapped_column(Integer)

    bid_present: Mapped[bool] = mapped_column(Boolean, default=False)
    ask_present: Mapped[bool] = mapped_column(Boolean, default=False)
    last_present: Mapped[bool] = mapped_column(Boolean, default=False)
    bid: Mapped[Decimal | None] = mapped_column(NUM)
    ask: Mapped[Decimal | None] = mapped_column(NUM)
    last_price: Mapped[Decimal | None] = mapped_column(NUM)
    # Plain string, not the MarketDataQuality DB enum -- a real IBKR
    # availability code can decode to "unavailable" (field 6509 = "N",
    # not subscribed), which isn't a member of that enum; this table
    # must never fail to persist a real, honest observation because of
    # an enum mismatch.
    market_data_quality: Mapped[str | None] = mapped_column(String(16))

    rate_limited: Mapped[bool] = mapped_column(Boolean, default=False)
    permission_error: Mapped[bool] = mapped_column(Boolean, default=False)
    # Phase 4 quote-observability hardening (2026-08-26), Section 10 -- a
    # sanitized category for a real provider exception that aborted this
    # capture before a normal poll row could be written: RATE_LIMITED,
    # PERMISSION_ERROR, AUTH_REQUIRED, CONTRACT_RESOLUTION_ERROR,
    # GATEWAY_TIMEOUT, GATEWAY_UNREACHABLE, or UNCLASSIFIED (see
    # services/entry_failure_taxonomy.py::classify_provider_exception).
    # None for a normal (non-exception) row -- every row before this
    # phase, and every successful poll row from here forward.
    provider_error_category: Mapped[str | None] = mapped_column(String(32))
    # False only for the one real contract-resolution row (attempt 0)
    # when no exact contract was found at all -- every real poll row
    # (attempt >= 1) only ever exists because resolution already
    # succeeded, so this is True for all of them by construction.
    contract_resolved: Mapped[bool] = mapped_column(Boolean, default=True)
    # True on the last real row persisted for this leg in this capture
    # attempt -- the one a diagnostic view should show by default,
    # without a reader needing to know this table's own polling
    # convention (highest snapshot_attempt_number) to find it.
    final_for_leg: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __repr__(self) -> str:
        return (
            f"QuoteAcquisitionAttempt(ticker={self.ticker!r}, "
            f"leg_index={self.leg_index!r}, attempt={self.snapshot_attempt_number!r})"
        )
