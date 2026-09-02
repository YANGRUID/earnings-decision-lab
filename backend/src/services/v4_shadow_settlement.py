"""V4.5 -- V4 shadow settlement observation.

WHAT THIS DOES. At the SAME legal exit window V3 uses, it observes the
real executable exit quotes for the already-frozen rank #1 candidate and
records the realized T+1 result.

WHAT IT MUST NOT DO (Sections 46, 48, 50, 52):
  * No re-ranking. The candidate was frozen at decision time; settlement
    observes it, it does not get to pick a better one after the event.
  * No replacement candidate. Choosing a different candidate post-event
    would be lookahead wearing a settlement costume.
  * No midpoint, no last-price fallback, no historical close, no
    intrinsic value, no theoretical model substituted for an absent
    observation. If the required side is unavailable in the legal
    window, that is recorded as a failure -- not repaired the next day.
  * No mutation of the frozen decision or its candidates. Outcome lives
    only here.

REALIZED RETURN (Section 84). Two different denominators exist and they
are NOT interchangeable, so both are stored under explicit names:

  * ``return_on_standardized_capital`` -- the T+1 benchmark objective's
    own convention, consistent with how V4.4A/V4.4B measure everything
    else. This is the comparable number.
  * ``realized_pnl`` -- raw currency, no denominator at all.

A theoretical expiration max-loss denominator is deliberately NOT used:
it belongs to a different (expiration-payoff) objective, and quietly
mixing it into a T+1 benchmark is precisely the objective mismatch V4
exists to correct.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from analytics.decision.v4_capital import PER_DECISION_CAPITAL
from models.v4_shadow import (
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowDecision,
    V4ShadowObservation,
    V4ShadowRunEvent,
    V4ShadowSettlement,
)
from providers.base import OptionsDataProvider
from providers.types import KnownContract


def _decimal(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def observe_shadow_settlement(
    db: Session,
    *,
    provider: OptionsDataProvider,
    decision: V4ShadowDecision,
    observed_at: datetime,
) -> V4ShadowSettlement:
    """Observes the legal exit window for one frozen shadow decision.

    Never raises at its boundary: a failure is persisted as an honest
    settlement record so the V3 path and the scheduler run are
    unaffected (Section 31).
    """
    existing = (
        db.query(V4ShadowSettlement).filter_by(shadow_decision_id=decision.id).one_or_none()
    )
    if existing is not None:
        # Section 51 -- exactly one successful settlement per decision.
        return existing

    def _fail(category: str, detail: str) -> V4ShadowSettlement:
        row = V4ShadowSettlement(
            shadow_decision_id=decision.id,
            settled_at=observed_at,
            status="OBSERVATION_FAILED",
            failure_category=category,
            failure_detail=detail,
        )
        db.add(row)
        db.add(
            V4ShadowRunEvent(
                shadow_decision_id=decision.id,
                earnings_calendar_event_id=decision.earnings_calendar_event_id,
                ticker=decision.ticker,
                occurred_at=observed_at,
                stage="settlement_observation",
                category=category,
                retryable=False,
                message=detail,
            )
        )
        db.flush()
        return row

    if decision.status != "RANKED" or not decision.rank_1_candidate_id:
        return _fail(
            "SETTLEMENT_OBSERVATION_FAILED",
            f"decision status is {decision.status} with no rank #1 candidate -- nothing to settle",
        )

    candidate = (
        db.query(V4ShadowCandidate)
        .filter_by(shadow_decision_id=decision.id, candidate_id=decision.rank_1_candidate_id)
        .one_or_none()
    )
    if candidate is None:
        return _fail("SETTLEMENT_OBSERVATION_FAILED", "frozen rank #1 candidate row not found")

    legs = (
        db.query(V4ShadowCandidateLeg)
        .filter_by(shadow_candidate_id=candidate.id)
        .order_by(V4ShadowCandidateLeg.leg_index)
        .all()
    )
    if not legs:
        return _fail("SETTLEMENT_OBSERVATION_FAILED", "frozen candidate has no legs")

    entry_obs = (
        db.query(V4ShadowObservation)
        .filter_by(shadow_decision_id=decision.id, phase="ENTRY")
        .one_or_none()
    )
    if entry_obs is None or entry_obs.net_executable_value is None:
        return _fail(
            "SETTLEMENT_OBSERVATION_FAILED",
            "no executable entry observation exists -- a T+1 result requires both sides of "
            "the round trip, and the entry side is never reconstructed after the fact",
        )

    # Re-quote the SAME frozen contracts by their own stable identifier.
    # Exact contracts only -- never a nearby strike, never a re-derived
    # geometry (the underlying has moved since entry, so re-selecting
    # strikes would silently settle a DIFFERENT position).
    #
    # A frozen leg with no conId cannot be re-quoted by identity at all.
    # That is a genuine, honest blocker: falling back to strike matching
    # would risk observing a different contract than the one frozen, so
    # it is reported rather than worked around.
    unidentifiable = [leg.leg_index for leg in legs if not leg.external_contract_id]
    if unidentifiable:
        return _fail(
            "SETTLEMENT_OBSERVATION_FAILED",
            f"frozen leg(s) {unidentifiable} carry no external contract id, so the exact "
            "contract cannot be re-quoted by identity; strike-matching could settle a "
            "different contract and is not permitted",
        )

    contracts = [
        KnownContract(
            strike=leg.strike,
            option_type=leg.right,
            external_contract_id=str(leg.external_contract_id),
            action=leg.action,
        )
        for leg in legs
    ]
    try:
        quotes = provider.get_quotes_for_known_contracts(
            decision.ticker, contracts, candidate.expiration, observed_at
        )
    except Exception as exc:  # noqa: BLE001 -- provider boundary
        return _fail(
            "SETTLEMENT_OBSERVATION_FAILED",
            f"exit quote acquisition failed: {type(exc).__name__}: {exc}",
        )

    by_contract = {(q.strike, q.option_type): q for q in quotes}

    exit_net = Decimal(0)
    missing: list[int] = []
    leg_rows: list[dict] = []
    qualities: set[str] = set()
    for leg in legs:
        quote = by_contract.get((leg.strike, leg.right))
        # Section 48 -- closing a long sells into the BID; closing a
        # short buys back at the ASK. No midpoint, no last fallback.
        side = "bid" if leg.action == "buy" else "ask"
        price = (
            (quote.bid if leg.action == "buy" else quote.ask) if quote is not None else None
        )
        if price is None:
            missing.append(leg.leg_index)
        else:
            sign = Decimal(1) if leg.action == "buy" else Decimal(-1)
            exit_net += sign * price * Decimal(leg.quantity) * (leg.multiplier or Decimal("100"))
        if quote is not None and quote.market_data_quality:
            qualities.add(quote.market_data_quality)
        leg_rows.append(
            {
                "leg_index": leg.leg_index,
                "action": leg.action,
                "right": leg.right,
                "strike": str(leg.strike),
                "required_side": side,
                "price": str(price) if price is not None else None,
                "bid": str(quote.bid) if quote and quote.bid is not None else None,
                "ask": str(quote.ask) if quote and quote.ask is not None else None,
                "external_contract_id": leg.external_contract_id,
                "market_data_quality": quote.market_data_quality if quote else None,
                "retrieved_at": quote.retrieved_at.isoformat() if quote else None,
            }
        )

    quality = (
        next(iter(qualities))
        if len(qualities) == 1
        else ("mixed:" + ",".join(sorted(qualities)) if qualities else None)
    )

    # The EXIT observation is persisted either way -- a failed settlement
    # still leaves auditable evidence of exactly what was and was not
    # observable in the legal window.
    db.add(
        V4ShadowObservation(
            shadow_decision_id=decision.id,
            phase="EXIT",
            candidate_id=candidate.candidate_id,
            observed_at=observed_at,
            status="NOT_EXECUTABLE" if missing else "OBSERVED",
            failure_category="SETTLEMENT_OBSERVATION_FAILED" if missing else None,
            failure_detail=(
                f"required exit side missing on leg(s) {missing} -- no midpoint, last-price, "
                "historical, or intrinsic substitution is permitted"
                if missing
                else None
            ),
            net_executable_value=None if missing else exit_net,
            market_data_quality=quality,
            source_provider="ibkr_tws",
            legs_json={"legs": leg_rows},
        )
    )

    if missing:
        db.flush()
        return _fail(
            "SETTLEMENT_OBSERVATION_FAILED",
            f"required exit side unavailable on leg(s) {missing} in the legal exit window",
        )

    entry_net = _decimal(entry_obs.net_executable_value) or Decimal(0)
    # Entry is a cost when positive (debit paid); exit is proceeds.
    realized = exit_net - entry_net
    row = V4ShadowSettlement(
        shadow_decision_id=decision.id,
        settled_at=observed_at,
        status="SETTLED",
        entry_net_value=entry_net,
        exit_net_value=exit_net,
        realized_pnl=realized,
        return_on_standardized_capital=realized / PER_DECISION_CAPITAL,
        market_data_quality=quality,
    )
    db.add(row)
    db.add(
        V4ShadowRunEvent(
            shadow_decision_id=decision.id,
            earnings_calendar_event_id=decision.earnings_calendar_event_id,
            ticker=decision.ticker,
            occurred_at=observed_at,
            stage="settlement_observation",
            category="OK",
            retryable=False,
            message=f"settled on observed executable exit quotes; realized {realized}",
        )
    )
    db.flush()
    return row
