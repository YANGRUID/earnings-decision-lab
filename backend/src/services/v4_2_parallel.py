"""V4.2 PARALLEL SHADOW -- the challenger's place in the 15:30 forward window.

Architecture, and why it is this and not another scheduler.

A second 15:30 ET job would race the control for the market-data lock and for
the window itself. The whole point of the challenger is that it observes the
SAME evidence package the control did, so a competing job would be both
dangerous and wrong. Instead the challenger runs as a THIRD PHASE inside the
existing forward-window coordinator, strictly after the control has finished:

    phase 1  control settlement      (due positions, priority)
    phase 2  control decisions       (new observations)
    phase 3  challenger              <- this module, last, flag-gated

Control priority is therefore structural rather than a promise. The challenger
cannot delay a control settlement, cannot delay a control decision, and cannot
take the market-data lock before either of them, because it does not begin
until both have returned.

Failure isolation is equally structural. Every challenger step here is wrapped
so that an exception becomes recorded challenger evidence and never reaches
the coordinator, and persistence uses SAVEPOINTs so a challenger rollback can
never unwind control work sharing the transaction.

Cost. The challenger's DECISION and ENTRY cost zero market-data requests: it
reads the control's own frozen candidates and their frozen quotes, so both
methodologies see identical strikes, identical spreads and identical modeled
economics, and any difference between them is attributable to the gate rather
than to one of them having seen better data. Only SETTLEMENT can require new
quotes, and only for contracts the control is not itself settling -- shared
contracts reuse the control's already-acquired evidence.

DecisionView is never called twice. The challenger reasons over the control's
frozen candidate set, which already embeds the single DecisionView of that
window. A future challenger that needed a different prompt or schema would be
a different methodology version, and would have to say so.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from models.v4_2_challenger import V42ChallengerDecision
from models.v4_shadow import V4ShadowDecision

log = logging.getLogger("services.v4_2_parallel")

CHALLENGER_HEALTH_DISABLED = "disabled"
CHALLENGER_HEALTH_READY = "ready"
CHALLENGER_HEALTH_RUNNING = "running"
CHALLENGER_HEALTH_DEGRADED = "degraded"
CHALLENGER_HEALTH_FAILED = "failed"

# Operational outcome vocabulary (Section 90). NO_ACTION is deliberately in
# the same list as the others and is NOT a failure: a methodology that
# declines when nothing clears its gates executed correctly.
OUTCOME_NO_ACTION = "NO_ACTION"
OUTCOME_ENTRY_CAPTURED = "ENTRY_CAPTURED"
OUTCOME_ENTRY_FAILED = "ENTRY_FAILED"
OUTCOME_WAITING_SETTLEMENT = "WAITING_SETTLEMENT"
OUTCOME_SETTLED = "SETTLED"
OUTCOME_SETTLEMENT_FAILED = "SETTLEMENT_FAILED"
OUTCOME_EOD_RECOVERED = "EOD_RECOVERED"

#: How far back a control decision may have been generated and still count as
#: belonging to THIS window. Generous enough to survive a late-starting or
#: slow window, far too short to reach a previous trading day.
WINDOW_LOOKBACK = timedelta(hours=6)

SUCCESSFUL_OUTCOMES = frozenset(
    {OUTCOME_NO_ACTION, OUTCOME_ENTRY_CAPTURED, OUTCOME_SETTLED, OUTCOME_EOD_RECOVERED}
)


@dataclass
class ChallengerPhaseSummary:
    """What the challenger phase did, in numbers Operations can display
    without joining anything."""

    enabled: bool = False
    evaluated: int = 0
    action: int = 0
    no_action: int = 0
    entries_observed: int = 0
    entries_failed: int = 0
    settled: int = 0
    settlement_failed: int = 0
    failed: int = 0
    market_data_requests: int = 0
    contracts_reused_from_control: int = 0
    #: Control decisions from earlier windows that were deliberately NOT
    #: given a retroactive challenger record.
    skipped_historical: int = 0
    by_event: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    # ---- Phase 2 (independent search), counted separately on purpose.
    # Merging the two phases' numbers would make the one question this phase
    # exists to answer -- did searching wider change what was chosen? --
    # unanswerable from the summary.
    phase2_enabled: bool = False
    phase2_evaluated: int = 0
    phase2_action: int = 0
    phase2_no_action: int = 0
    phase2_failed: int = 0
    #: Events whose legal decision window precedes the Phase-2 activation
    #: instant. Reported, never silently skipped.
    phase2_skipped_not_activated: int = 0
    #: Events not STARTED because the forward window's deadline had passed.
    #: An event already in flight is always finished.
    phase2_deadline_skipped: int = 0
    phase2_market_data_requests: int = 0
    phase2_expiries_searched: int = 0
    phase2_candidates_evaluated: int = 0
    phase2_distinct_selections: int = 0
    phase2_by_event: dict[str, str] = field(default_factory=dict)

    @property
    def health(self) -> str:
        if not (self.enabled or self.phase2_enabled):
            return CHALLENGER_HEALTH_DISABLED
        if self.failed and not (self.evaluated - self.failed):
            return CHALLENGER_HEALTH_FAILED
        if (
            self.failed
            or self.settlement_failed
            or self.entries_failed
            or self.phase2_failed
        ):
            return CHALLENGER_HEALTH_DEGRADED
        return CHALLENGER_HEALTH_READY


def run_challenger_phase(
    db: Session,
    settings: Any,
    *,
    provider: Any,
    now: datetime,
    shared_exit_quotes: dict[str, Any] | None = None,
    settlement_date: date | None = None,
    dry_run: bool = False,
    deadline: datetime | None = None,
    clock: Any = None,
) -> ChallengerPhaseSummary:
    """The challenger's whole turn in one window: settle what is due, then
    evaluate and freeze what is new.

    Settlement runs BEFORE evaluation for the same reason the control's does:
    a position whose legal exit window is open now must not be queued behind
    new analysis. Within the challenger this ordering is its own; it can never
    reorder anything the control does, because the control has already
    finished by the time this is called.

    Never raises. A challenger fault is recorded and the window continues.
    """
    summary = ChallengerPhaseSummary(
        enabled=bool(getattr(settings, "v4_2_parallel_enabled", False)),
        phase2_enabled=bool(getattr(settings, "v4_2_independent_search_enabled", False)),
    )
    if not (summary.enabled or summary.phase2_enabled):
        return summary

    try:
        summary.settled, summary.settlement_failed = _settle_due_challengers(
            db,
            provider=provider,
            now=now,
            shared_exit_quotes=shared_exit_quotes or {},
            summary=summary,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001 -- never reaches the coordinator
        log.error("challenger settlement phase failed", exc_info=True)
        summary.errors.append(f"settlement: {type(exc).__name__}: {exc}")
        summary.failed += 1

    if summary.enabled:
        try:
            _evaluate_new_decisions(
                db,
                provider=provider,
                now=now,
                settlement_date=settlement_date,
                summary=summary,
                dry_run=dry_run,
                activation_at=getattr(settings, "v4_2_parallel_activation_at", None),
            )
        except Exception as exc:  # noqa: BLE001
            log.error("challenger evaluation phase failed", exc_info=True)
            summary.errors.append(f"evaluation: {type(exc).__name__}: {exc}")
            summary.failed += 1

    # Phase 2 runs LAST, after Phase 1 has already returned. Ordering, not a
    # promise: the independent search issues real market-data requests and is
    # the most expensive thing in the window, and it must not be able to delay
    # the control's settlement, the control's decisions, or Phase 1's record.
    if summary.phase2_enabled:
        try:
            _evaluate_phase2_decisions(
                db,
                settings=settings,
                provider=provider,
                now=now,
                settlement_date=settlement_date,
                summary=summary,
                dry_run=dry_run,
                deadline=deadline,
                clock=clock,
            )
        except Exception as exc:  # noqa: BLE001
            log.error("phase-2 evaluation failed", exc_info=True)
            summary.errors.append(f"phase2: {type(exc).__name__}: {exc}")
            summary.phase2_failed += 1

    return summary


def _settle_due_challengers(
    db: Session,
    *,
    provider: Any,
    now: datetime,
    shared_exit_quotes: dict[str, Any],
    summary: ChallengerPhaseSummary,
    dry_run: bool,
) -> tuple[int, int]:
    """Settle every challenger position whose own event's legal exit window is
    open, using the CONTROL's window rules verbatim."""
    from models.earnings_calendar_event import EarningsCalendarEvent  # noqa: PLC0415
    from models.v4_2_challenger import (  # noqa: PLC0415
        V42ChallengerConfigEntry,
        V42ChallengerConfigSettlement,
    )
    from services.v4_2_challenger_settlement import (  # noqa: PLC0415
        fail_missed_challenger_window,
        settle_challenger_decision,
    )
    from services.v4_shadow_scheduler import (  # noqa: PLC0415
        SETTLEMENT_DUE,
        SETTLEMENT_NOT_DUE,
        v4_schedule_for_event,
        v4_settlement_window_state,
    )

    settled = failed = 0
    settled_ids = db.query(V42ChallengerConfigSettlement.challenger_config_result_id).filter(
        V42ChallengerConfigSettlement.status == "SETTLED"
    )
    pending_ids = {
        e.challenger_decision_id
        for e in db.query(V42ChallengerConfigEntry)
        .filter(V42ChallengerConfigEntry.status == "OBSERVED")
        .filter(~V42ChallengerConfigEntry.challenger_config_result_id.in_(settled_ids))
    }
    if not pending_ids:
        return 0, 0

    decisions = (
        db.query(V42ChallengerDecision)
        .filter(V42ChallengerDecision.id.in_(pending_ids))
        .order_by(V42ChallengerDecision.id)
        .all()
    )
    events = {
        e.id: e
        for e in db.query(EarningsCalendarEvent).filter(
            EarningsCalendarEvent.id.in_([d.earnings_calendar_event_id for d in decisions] or [0])
        )
    }

    for decision in decisions:
        savepoint = db.begin_nested()
        try:
            event = events.get(decision.earnings_calendar_event_id)
            if event is None:
                state = "MISSED"
            else:
                state = v4_settlement_window_state(event, now)
            if state == SETTLEMENT_NOT_DUE:
                savepoint.rollback()
                continue
            if state == SETTLEMENT_DUE:
                if dry_run:
                    savepoint.rollback()
                    continue
                result = settle_challenger_decision(
                    db,
                    provider=provider,
                    challenger=decision,
                    observed_at=now,
                    shared_quotes=shared_exit_quotes,
                )
                settled += result.settled
                failed += result.failed
                summary.contracts_reused_from_control += result.contracts_reused_from_control
                summary.market_data_requests += result.quote_calls
                summary.by_event[decision.ticker] = (
                    OUTCOME_SETTLED
                    if result.settled and not result.failed
                    else OUTCOME_SETTLEMENT_FAILED
                )
            else:
                if dry_run:
                    savepoint.rollback()
                    continue
                detail = (
                    "legal settlement window "
                    f"{v4_schedule_for_event(event).exit_timestamp.isoformat()} had already "
                    f"passed at {now.isoformat()}"
                    if event is not None
                    else "calendar event no longer exists; no legal exit window can be established"
                )
                failed += fail_missed_challenger_window(
                    db, challenger=decision, observed_at=now, detail=detail
                )
                summary.by_event[decision.ticker] = OUTCOME_SETTLEMENT_FAILED
            savepoint.commit()
        except Exception as exc:  # noqa: BLE001 -- isolate per decision
            savepoint.rollback()
            log.error("challenger settlement failed for %s", decision.ticker, exc_info=True)
            summary.errors.append(f"{decision.ticker}: {type(exc).__name__}: {exc}")
            failed += 1
    return settled, failed


def _evaluate_new_decisions(
    db: Session,
    *,
    provider: Any,
    now: datetime,
    settlement_date: date | None,
    summary: ChallengerPhaseSummary,
    dry_run: bool,
    activation_at: datetime | None = None,
) -> None:
    """Evaluate and freeze a challenger decision for every control decision
    generated in THIS window that does not have one yet.

    The window bound is the important part, and it is not an optimisation.
    Without it, the first run after the flag is turned on would sweep up every
    control decision ever made and manufacture challenger "forward evidence"
    for events whose outcomes are already known -- evidence that looks
    prospective in the table and is nothing of the kind. Parallel shadow means
    the challenger observes the same events the control observes, going
    forward, and an event the control decided last week is simply not one of
    them.
    """
    # Scoped to Phase-1 rows. Both phases record against the same control
    # decision by design, so an unscoped set would let whichever phase ran
    # first suppress the other -- Phase 2 running would silently stop Phase 1
    # producing its own evidence, which is the opposite of an overlap period.
    from models.v4_2_challenger import phase_1_rows  # noqa: PLC0415
    from services.v4_2_challenger import (  # noqa: PLC0415
        CHALLENGER_STATUS_ALREADY_FROZEN,
        CHALLENGER_STATUS_FAILED,
        CHALLENGER_STATUS_RANKED,
        evaluate_and_freeze,
    )
    from services.v4_2_challenger_entry import freeze_challenger_entries  # noqa: PLC0415

    existing = {
        row[0]
        for row in db.query(V42ChallengerDecision.shadow_decision_id)
        .filter(V42ChallengerDecision.shadow_decision_id.isnot(None))
        .filter(phase_1_rows())
    }
    # Two guards, and the stricter one wins. The lookback keeps the challenger
    # inside THIS window; the activation boundary is an absolute floor that no
    # amount of clock drift, restart or replay can slip beneath. Together they
    # are what makes "no historical backfill" a property rather than a hope.
    window_start = now - WINDOW_LOOKBACK
    if activation_at is not None and activation_at > window_start:
        window_start = activation_at
    controls = (
        db.query(V4ShadowDecision)
        .filter(V4ShadowDecision.id.notin_(existing or [0]))
        .filter(V4ShadowDecision.generated_at >= window_start)
        .order_by(V4ShadowDecision.id)
        .all()
    )
    skipped = (
        db.query(V4ShadowDecision)
        .filter(V4ShadowDecision.id.notin_(existing or [0]))
        .filter(V4ShadowDecision.generated_at < window_start)
        .count()
    )
    if skipped:
        summary.skipped_historical = skipped
        log.info(
            "v4.2 challenger: %d pre-existing control decision(s) left alone "
            "(boundary %s) -- a challenger record is only ever created prospectively",
            skipped,
            window_start.isoformat(),
        )
    for control in controls:
        try:
            evaluation = evaluate_and_freeze(
                db, control, settlement_date=settlement_date, dry_run=dry_run
            )
            summary.evaluated += 1
            summary.market_data_requests += evaluation.market_data_requests_issued
            summary.contracts_reused_from_control += evaluation.unique_contracts_reused
            if evaluation.status == CHALLENGER_STATUS_FAILED:
                summary.failed += 1
                summary.by_event[control.ticker] = CHALLENGER_STATUS_FAILED
                continue
            if evaluation.status == CHALLENGER_STATUS_RANKED:
                summary.action += 1
            elif evaluation.status != CHALLENGER_STATUS_ALREADY_FROZEN:
                summary.no_action += 1
                summary.by_event[control.ticker] = OUTCOME_NO_ACTION
            if dry_run or evaluation.decision_id is None:
                continue

            frozen = db.get(V42ChallengerDecision, evaluation.decision_id)
            if frozen is None:  # pragma: no cover -- defensive
                continue
            entry = freeze_challenger_entries(
                db, challenger=frozen, settlement_date=settlement_date
            )
            summary.entries_observed += entry.entries_observed
            summary.entries_failed += entry.entries_failed
            if entry.entries_observed:
                summary.by_event[control.ticker] = OUTCOME_ENTRY_CAPTURED
            elif entry.entries_failed:
                summary.by_event[control.ticker] = OUTCOME_ENTRY_FAILED
        except Exception as exc:  # noqa: BLE001 -- one event never fails the phase
            log.error("challenger evaluation failed for %s", control.ticker, exc_info=True)
            summary.errors.append(f"{control.ticker}: {type(exc).__name__}: {exc}")
            summary.failed += 1


def _evaluate_phase2_decisions(
    db: Session,
    *,
    settings: Any,
    provider: Any,
    now: datetime,
    settlement_date: date | None,
    summary: ChallengerPhaseSummary,
    dry_run: bool,
    deadline: datetime | None = None,
    clock: Any = None,
) -> None:
    """Run the INDEPENDENT SEARCH for every control decision in this window.

    Phase 2 shares the control's window and the control's DecisionView and
    nothing else: it builds its own bounded multi-expiry universe, values it,
    and lets the six configurations choose within it.

    Three guards stand between this and retroactive evidence, and the
    strictest wins. The window lookback keeps it inside THIS window. The
    activation instant is an absolute floor no clock drift or restart can slip
    beneath. And the floor is checked against the control's own LEGAL DECISION
    WINDOW rather than the moment this code runs, so a late or retried run
    cannot admit an event whose window opened before activation.
    """
    from analytics.decision.v4_2_phase2_methodology import (  # noqa: PLC0415
        PHASE_2_METHODOLOGY,
    )
    from services.v4_2_challenger_entry import freeze_challenger_entries  # noqa: PLC0415
    from services.v4_2_phase2 import (  # noqa: PLC0415
        PHASE2_STATUS_ACTION,
        PHASE2_STATUS_FAILED,
        run_independent_search,
    )
    from services.v4_2_phase2_evidence import (  # noqa: PLC0415
        FREEZE_FROZEN,
        freeze_phase2_decision,
        phase2_activated,
    )

    enabled = bool(getattr(settings, "v4_2_independent_search_enabled", False))
    activation_at = getattr(settings, "v4_2_independent_search_activation_at", None)
    max_variants = int(getattr(settings, "v4_2_independent_search_max_expiries", 3) or 3)

    existing = {
        row[0]
        for row in db.query(V42ChallengerDecision.shadow_decision_id)
        .filter(V42ChallengerDecision.shadow_decision_id.isnot(None))
        .filter(V42ChallengerDecision.methodology_version == PHASE_2_METHODOLOGY)
    }
    # The lookback bounds the SWEEP; the activation instant is enforced per
    # event below, against that event's own legal decision window. Deliberately
    # not folded into this query: an event excluded by a WHERE clause is
    # invisible, and a boundary that silently drops work is one nobody can
    # audit. Enforced once, in one place, and counted.
    window_start = now - WINDOW_LOOKBACK

    controls = (
        db.query(V4ShadowDecision)
        .filter(V4ShadowDecision.id.notin_(existing or [0]))
        .filter(V4ShadowDecision.generated_at >= window_start)
        .order_by(V4ShadowDecision.id)
        .all()
    )

    tick = clock or (lambda: datetime.now(UTC))
    for control in controls:
        # The window's own deadline, obeyed the way the control obeys it: stop
        # STARTING new events, never abandon one already in flight. Phase 2 is
        # the only challenger work that opens market-data subscriptions, so it
        # is the only one that could push a window past its close.
        if deadline is not None and tick() >= deadline:
            summary.phase2_deadline_skipped += 1
            summary.phase2_by_event[control.ticker] = "DEADLINE_SKIPPED"
            log.info(
                "phase 2 skipped %s: the %s forward-window deadline had passed before this "
                "event was started",
                control.ticker,
                deadline.isoformat(),
            )
            continue

        allowed, why = phase2_activated(
            enabled=enabled,
            activation_at=activation_at,
            legal_decision_window_at=control.legal_decision_window_at or control.generated_at,
        )
        if not allowed:
            summary.phase2_skipped_not_activated += 1
            summary.phase2_by_event[control.ticker] = "NOT_ACTIVATED"
            log.info("phase 2 skipped %s: %s", control.ticker, why)
            continue

        try:
            evaluation = run_independent_search(
                db,
                provider=provider,
                decision=control,
                settlement_date=settlement_date or now.date(),
                as_of=now,
                max_variants=max_variants,
                control_contract_ids=_control_contract_ids(db, control),
            )
            summary.phase2_evaluated += 1
            summary.phase2_market_data_requests += evaluation.telemetry.total_requests
            summary.phase2_expiries_searched += evaluation.expiries_considered
            summary.phase2_candidates_evaluated += len(evaluation.universe)
            summary.phase2_distinct_selections += len(evaluation.selected_candidate_ids)

            if evaluation.status == PHASE2_STATUS_FAILED:
                summary.phase2_failed += 1
                summary.phase2_by_event[control.ticker] = PHASE2_STATUS_FAILED
            elif evaluation.status == PHASE2_STATUS_ACTION:
                summary.phase2_action += 1
                summary.phase2_by_event[control.ticker] = (
                    f"{evaluation.action_count}/6 ACTION, "
                    f"{len(evaluation.selected_candidate_ids)} distinct"
                )
            else:
                summary.phase2_no_action += 1
                summary.phase2_by_event[control.ticker] = OUTCOME_NO_ACTION

            if dry_run:
                continue

            frozen = freeze_phase2_decision(
                db,
                control,
                evaluation,
                settlement_date=settlement_date,
            )
            if frozen.status != FREEZE_FROZEN or frozen.decision_id is None:
                continue
            row = db.get(V42ChallengerDecision, frozen.decision_id)
            if row is None:  # pragma: no cover -- defensive
                continue
            entry = freeze_challenger_entries(
                db, challenger=row, settlement_date=settlement_date
            )
            summary.entries_observed += entry.entries_observed
            summary.entries_failed += entry.entries_failed
        except Exception as exc:  # noqa: BLE001 -- one event never fails the phase
            log.error("phase-2 evaluation failed for %s", control.ticker, exc_info=True)
            summary.errors.append(f"phase2 {control.ticker}: {type(exc).__name__}: {exc}")
            summary.phase2_failed += 1


def _control_contract_ids(db: Session, control: V4ShadowDecision) -> set[str]:
    """Contracts the CONTROL already observed in this same window. Used only
    to report honestly how much of the independent search was new -- never to
    restrict what Phase 2 may look at."""
    from models.v4_shadow import V4ShadowCandidate, V4ShadowCandidateLeg  # noqa: PLC0415

    rows = (
        db.query(V4ShadowCandidateLeg.external_contract_id)
        .join(V4ShadowCandidate, V4ShadowCandidateLeg.shadow_candidate_id == V4ShadowCandidate.id)
        .filter(V4ShadowCandidate.shadow_decision_id == control.id)
        .distinct()
        .all()
    )
    return {str(r[0]) for r in rows if r[0]}

__all__ = [
    "CHALLENGER_HEALTH_DEGRADED",
    "CHALLENGER_HEALTH_DISABLED",
    "CHALLENGER_HEALTH_FAILED",
    "CHALLENGER_HEALTH_READY",
    "CHALLENGER_HEALTH_RUNNING",
    "OUTCOME_ENTRY_CAPTURED",
    "OUTCOME_ENTRY_FAILED",
    "OUTCOME_EOD_RECOVERED",
    "OUTCOME_NO_ACTION",
    "OUTCOME_SETTLED",
    "OUTCOME_SETTLEMENT_FAILED",
    "OUTCOME_WAITING_SETTLEMENT",
    "ChallengerPhaseSummary",
    "run_challenger_phase",
]
