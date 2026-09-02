"""Options Decision Engine V4.3 -- Real V3 Strike Replay (2026-09-02).

Replays real, already-frozen V3 DecisionSnapshot rows through V4.3's
expected-move-aware strike engine, using ONLY decision-time information
(direction-implied strategy_type, underlying_price, selected
expiration, the real captured options chain at that decision's own
grounding snapshot, and real prior-earnings historical move data) --
never realized stock move, realized P&L, settlement outcome, or
post-event IV. Mirrors v4_replay.py's own Section 19 anti-lookahead
rule: this module never queries a realized-outcome table and its
return type carries no such field, so the rule cannot be violated by
construction, not merely by convention.

NOT a counterfactual P&L simulation. It answers exactly one question
per decision: "what strikes would V4.3's expected-move-aware engine
have chosen, using only what was knowable at decision time, compared
to what V3 actually selected?" Real settled outcomes may only be
compared AFTERWARD, by a human or a report, never fed back into this
module.

Some real historical decisions cannot be honestly replayed -- e.g. one
real ticker in this dataset has no persisted volatility_snapshot at
all (no implied move was ever computed for it at decision time), so no
honest ExpectedMoveContext can be reconstructed. These are marked
``replayable=False`` with a real ``CANNOT_REPLAY_HONESTLY`` reason,
never silently skipped or reconstructed with information that wasn't
genuinely available at decision time (see the V4.3 report's own
replay section for which real ticker this applies to and why).

Pure function over already-known inputs -- no DB session, no live
call. The real dataset used to produce the V4.3 report was queried
once, read-only, directly from the production database, and is NOT
hardcoded here as a fixture module -- this module is the
general-purpose replay function any real or synthetic decision-shaped
input can be run through.
"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal

from analytics.decision.v4_expected_move import derive_expected_move_context
from analytics.decision.v4_strike_engine import V4StrikeSelectionResult, select_v4_strikes
from analytics.options.strategy_candidates import StrategyCategory
from providers.types import OptionQuote

# (action, option_type, strike) -- the real strikes V3 actually
# selected for one leg, exactly as frozen on the real DecisionSnapshot.
V3LegSummary = tuple[str, str, Decimal]

# V4.3.1 (Sections 4/5/21) -- V4.3 reason codes that could reflect
# either a genuine real-chain boundary OR merely this replay's own
# narrow captured-window limits. Historical replay can never tell
# those apart (see v4_chain_coverage.py's own module docstring), so
# any of these trigger the honest CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW
# caveat rather than letting the result read as a confirmed absence.
_CHAIN_COVERAGE_SENSITIVE_CODES = frozenset(
    {"UNCONSTRUCTABLE_NO_PROTECTIVE_WING_AVAILABLE", "TARGET_BEYOND_AVAILABLE_CHAIN"}
)

CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW_CAVEAT = (
    "CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW -- this decision's chain data is a narrow "
    "captured window (never complete listed-strike metadata); a target/wing landing at or "
    "beyond that window's edge is not proof the real historical listed chain lacked a "
    "strike there, only that this window never asked. See v4_chain_coverage.py."
)


def _coverage_caveat_for(result: V4StrikeSelectionResult) -> str | None:
    """Returns the caveat above when ``result`` carries any chain-
    coverage-sensitive reason code (top-level or on any leg), else
    None. A pure text annotation -- never changes ``result.status`` or
    any other V4.3 field, and never claims the real chain DID have (or
    lacked) a strike; it only flags that this replay cannot know."""
    top_level_codes = set(result.reason_codes)
    leg_codes = {code for leg in result.legs for code in leg.reason_codes}
    if not (top_level_codes | leg_codes) & _CHAIN_COVERAGE_SENSITIVE_CODES:
        return None
    return CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW_CAVEAT


@dataclass(frozen=True)
class V3StrikeReplayInput:
    """Decision-time-only fields needed to replay one real (or
    synthetic, for testing) V3 decision through V4.3 -- deliberately
    excludes every settlement/outcome field that exists on the real
    row."""

    ticker: str
    strategy_type: str | None
    underlying_price: Decimal | None
    observed_at: datetime | None
    expiration: date | None
    v3_legs: tuple[V3LegSummary, ...]
    chain_quotes: tuple[OptionQuote, ...] | None
    historical_next_day_move_pcts: tuple[Decimal, ...] | None


@dataclass(frozen=True)
class V3StrikeReplayResult:
    ticker: str
    strategy_type: str | None
    v3_legs: tuple[V3LegSummary, ...]
    v4_result: V4StrikeSelectionResult | None
    replayable: bool
    skip_reason: str | None
    # V4.3.1 -- CANNOT_VERIFY_OUTSIDE_CAPTURED_WINDOW_CAVEAT when the
    # result touched the captured window's edge, else None. See
    # _coverage_caveat_for's own docstring.
    coverage_caveat: str | None


def replay_v3_strikes(decision: V3StrikeReplayInput) -> V3StrikeReplayResult:
    if not decision.strategy_type:
        return V3StrikeReplayResult(
            ticker=decision.ticker,
            strategy_type=None,
            v3_legs=(),
            v4_result=None,
            replayable=False,
            skip_reason="NO_ACTION -- no strategy was selected, nothing to replay.",
            coverage_caveat=None,
        )

    try:
        category = StrategyCategory(decision.strategy_type)
    except ValueError:
        return V3StrikeReplayResult(
            ticker=decision.ticker,
            strategy_type=decision.strategy_type,
            v3_legs=decision.v3_legs,
            v4_result=None,
            replayable=False,
            skip_reason=f"Unrecognized strategy_type {decision.strategy_type!r}.",
            coverage_caveat=None,
        )

    if decision.underlying_price is None or decision.observed_at is None:
        return V3StrikeReplayResult(
            ticker=decision.ticker,
            strategy_type=decision.strategy_type,
            v3_legs=decision.v3_legs,
            v4_result=None,
            replayable=False,
            skip_reason=(
                "CANNOT_REPLAY_HONESTLY -- no real decision-time underlying price/observation "
                "timestamp is on record for this decision."
            ),
            coverage_caveat=None,
        )

    if not decision.chain_quotes:
        return V3StrikeReplayResult(
            ticker=decision.ticker,
            strategy_type=decision.strategy_type,
            v3_legs=decision.v3_legs,
            v4_result=None,
            replayable=False,
            skip_reason=(
                "CANNOT_REPLAY_HONESTLY -- no real captured options chain survives for this "
                "decision's own grounding snapshot."
            ),
            coverage_caveat=None,
        )

    context = derive_expected_move_context(
        spot=decision.underlying_price,
        observed_at=decision.observed_at,
        expiration=decision.expiration,
        quotes_for_expiration=list(decision.chain_quotes),
        historical_next_day_move_pcts=(
            list(decision.historical_next_day_move_pcts)
            if decision.historical_next_day_move_pcts
            else None
        ),
    )
    result = select_v4_strikes(category, context, list(decision.chain_quotes))

    return V3StrikeReplayResult(
        ticker=decision.ticker,
        strategy_type=decision.strategy_type,
        v3_legs=decision.v3_legs,
        v4_result=result,
        replayable=True,
        skip_reason=None,
        coverage_caveat=_coverage_caveat_for(result),
    )


def replay_many_strikes(decisions: list[V3StrikeReplayInput]) -> list[V3StrikeReplayResult]:
    return [replay_v3_strikes(d) for d in decisions]
