"""V4.2 Phase 1 -- a configuration may never hold more risk than its own cap.

The defect these close (2026-09-24). ``evaluate_challenger`` passed
``entry_cash_by_candidate`` to the per-configuration check and omitted
``max_loss_by_candidate``, which defaults to ``{}``, so
``assess_configuration_fit`` saw ``max_loss_dollars=None`` for every candidate
and ``RISK_CAP_EXCEEDED`` could not fire. It fired zero times across 90
production configuration rows.

That reached the frozen evidence. ``size_configuration_position`` floors
quantity at one contract for a candidate its own docstring calls
"already-ELIGIBLE (one contract is known to fit)"; nothing had checked, so
four of the twelve challenger entries were written holding two to three times
their cap -- DRI and PAYX $2K Conservative at $850 and $930 against $300.

The fixture reproduces those exact numbers: a wide put credit spread risking
$930 a contract and a narrower one risking $400, on one event.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from analytics.decision.v4_configurations import V4_CONFIGURATIONS, get_configuration
from analytics.decision_timing_policy import V4_TIMING_POLICY
from models.earnings_calendar_event import EarningsCalendarEvent
from models.v4_2_challenger import V42ChallengerConfigEntry, V42ChallengerConfigResult
from models.v4_shadow import (
    SHADOW_SCHEMA_VERSION,
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowDecision,
)
from services.v4_2_challenger import evaluate_and_freeze, evaluate_challenger
from services.v4_2_challenger_entry import freeze_challenger_entries

D = Decimal
OBSERVED = datetime(2026, 9, 24, 19, 30, tzinfo=UTC)
EXPIRY = date(2026, 10, 16)

#: (candidate_id, median, legs) -- legs are (action, right, strike, bid, ask).
#: WIDE risks $930 a contract, NARROW $400, arithmetic verified against
#: analytics/options/payoff.py::analyze.
WIDE = "put_credit_spread:WIDER_WING"
NARROW = "put_credit_spread:MINIMUM_WIDTH"
_CANDIDATES = (
    (
        WIDE, "W", "0.12",
        (("sell", "put", "100", "1.00", "1.10"), ("buy", "put", "90", "0.20", "0.30")),
    ),
    (
        NARROW, "N", "0.06",
        (("sell", "put", "100", "1.20", "1.30"), ("buy", "put", "95", "0.10", "0.20")),
    ),
)


@pytest.fixture
def control(db_session):
    """One event whose best structure risks more than the two smallest caps
    allow, and whose second-best fits one of them."""
    event = EarningsCalendarEvent(
        symbol="RSKCAP", company_name="Risk Cap Co", earnings_date=date(2026, 9, 24),
        earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    decision = V4ShadowDecision(
        earnings_calendar_event_id=event.id, ticker="RSKCAP", company_name="Risk Cap Co",
        legal_decision_window_at=OBSERVED, generated_at=OBSERVED, as_of=OBSERVED,
        status="RANKED", engine_version="v4-test",
        shadow_schema_version=SHADOW_SCHEMA_VERSION,
        decision_timing_policy_version=V4_TIMING_POLICY.version,
        candidate_count=2, rankable_candidate_count=2,
        underlying_price=D("100"), market_data_quality="delayed",
        expected_move={"implied_move_pct": "0.05"},
    )
    db_session.add(decision)
    db_session.flush()

    for candidate_id, conid_prefix, median, legs in _CANDIDATES:
        candidate = V4ShadowCandidate(
            shadow_decision_id=decision.id, candidate_id=candidate_id,
            strategy="put_credit_spread", expiration=EXPIRY, validity_status="RANKABLE",
            semantic_compatibility=D("1.0"), semantic_tier="strong",
            core_median_return=D(median), core_worst_return=D("-0.20"),
            core_best_return=D("0.25"), core_positive_scenario_fraction=D("0.60"),
            no_profitable_region=False, mean_relative_spread=D("0.05"),
            capital_utilisation=D("0.05"), entry_cash_required=D("0"),
        )
        db_session.add(candidate)
        db_session.flush()
        for index, (action, right, strike, bid, ask) in enumerate(legs):
            db_session.add(
                V4ShadowCandidateLeg(
                    shadow_candidate_id=candidate.id, leg_index=index, action=action,
                    right=right, strike=D(strike), quantity=1, multiplier=D("100"),
                    external_contract_id=f"{conid_prefix}{index}",
                    bid=D(bid), ask=D(ask), market_data_quality="delayed",
                )
            )
        db_session.flush()
    return decision


def _by_key(config_rows):
    return {row["configuration_key"]: row for row in config_rows}


class TestTheCapBinds:
    def test_a_configuration_whose_cap_admits_nothing_declines(self, db_session, control):
        """$2K Conservative allows $300; the cheaper structure still risks
        $400. Before the fix this configuration actioned the $930 one."""
        rows = _by_key(evaluate_challenger(db_session, control).config_rows)

        conservative = rows["v4_2k_conservative"]
        assert conservative["status"] == "NO_ACTION"
        assert conservative["selected_candidate_id"] is None
        assert "RISK_CAP_EXCEEDED" in conservative["no_action_reason"]

    def test_a_configuration_takes_the_structure_that_fits_its_cap(self, db_session, control):
        """$2K Moderate allows $600. The best structure risks $930 and is
        refused; the configuration keeps ranking and takes the $400 one rather
        than declining the event."""
        rows = _by_key(evaluate_challenger(db_session, control).config_rows)

        assert rows["v4_2k_moderate"]["status"] == "RANKED"
        assert rows["v4_2k_moderate"]["selected_candidate_id"] == NARROW

    def test_a_configuration_that_can_hold_the_best_structure_still_does(
        self, db_session, control
    ):
        rows = _by_key(evaluate_challenger(db_session, control).config_rows)

        for key in ("v4_2k_aggressive", "v4_10k_conservative", "v4_10k_moderate",
                    "v4_10k_aggressive"):
            assert rows[key]["status"] == "RANKED"
            assert rows[key]["selected_candidate_id"] == WIDE, (
                f"{key} can afford the best structure and should still take it"
            )

    def test_the_configurations_no_longer_all_agree(self, db_session, control):
        """The measured symptom: 90 production configuration rows, never once
        a divergence. Two structures and a declining configuration is three
        distinct answers from the same evidence."""
        rows = evaluate_challenger(db_session, control).config_rows

        selected = {r["selected_candidate_id"] for r in rows}
        assert selected == {WIDE, NARROW, None}


class TestNoPersistedEntryExceedsItsCap:
    """The invariant, asserted on what is actually written."""

    def test_every_frozen_entry_is_within_its_configuration_cap(self, db_session, control):
        evaluation = evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()
        assert evaluation.decision_id is not None

        from models.v4_2_challenger import V42ChallengerDecision  # noqa: PLC0415

        challenger = db_session.get(V42ChallengerDecision, evaluation.decision_id)
        freeze_challenger_entries(db_session, challenger=challenger, settlement_date=EXPIRY)
        db_session.flush()

        entries = (
            db_session.query(V42ChallengerConfigEntry)
            .filter_by(challenger_decision_id=challenger.id)
            .all()
        )
        assert entries, "nothing was frozen, so the invariant would pass vacuously"
        for entry in entries:
            cap = get_configuration(entry.configuration_key).max_risk_dollars
            assert entry.max_risk_used is not None
            assert entry.max_risk_used <= cap, (
                f"{entry.configuration_key} froze ${entry.max_risk_used} against a ${cap} cap"
            )
            assert entry.quantity >= 1

    def test_the_declining_configuration_freezes_no_entry_at_all(self, db_session, control):
        evaluation = evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()

        from models.v4_2_challenger import V42ChallengerDecision  # noqa: PLC0415

        challenger = db_session.get(V42ChallengerDecision, evaluation.decision_id)
        freeze_challenger_entries(db_session, challenger=challenger, settlement_date=EXPIRY)
        db_session.flush()

        keys = {
            e.configuration_key
            for e in db_session.query(V42ChallengerConfigEntry).filter_by(
                challenger_decision_id=challenger.id
            )
        }
        assert "v4_2k_conservative" not in keys
        assert len(keys) == 5

    def test_every_configuration_is_still_answered(self, db_session, control):
        """A refusal is an answer. All six configurations get a row whether or
        not they acted -- the declining one is evidence, not an omission."""
        evaluation = evaluate_and_freeze(db_session, control, dry_run=False)
        db_session.flush()

        rows = (
            db_session.query(V42ChallengerConfigResult)
            .filter_by(challenger_decision_id=evaluation.decision_id)
            .all()
        )
        assert {r.configuration_key for r in rows} == {c.key for c in V4_CONFIGURATIONS}


class TestTheCapIsNotAppliedWhereItShouldNotBe:
    def test_an_event_every_configuration_can_hold_is_unaffected(self, db_session):
        """The fix must refuse structures that exceed a cap, not shrink the
        challenger generally. A cheap structure is still actioned six times."""
        event = EarningsCalendarEvent(
            symbol="RSKOK", company_name="Cheap Co", earnings_date=date(2026, 9, 24),
            earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
        )
        db_session.add(event)
        db_session.flush()
        decision = V4ShadowDecision(
            earnings_calendar_event_id=event.id, ticker="RSKOK", company_name="Cheap Co",
            legal_decision_window_at=OBSERVED, generated_at=OBSERVED, as_of=OBSERVED,
            status="RANKED", engine_version="v4-test",
            shadow_schema_version=SHADOW_SCHEMA_VERSION,
            decision_timing_policy_version=V4_TIMING_POLICY.version,
            candidate_count=1, rankable_candidate_count=1,
            underlying_price=D("100"), market_data_quality="delayed",
            expected_move={"implied_move_pct": "0.05"},
        )
        db_session.add(decision)
        db_session.flush()
        # Width 1, credit 0.80 -> max loss $20 a contract: inside every cap.
        candidate = V4ShadowCandidate(
            shadow_decision_id=decision.id, candidate_id="put_credit_spread:TINY",
            strategy="put_credit_spread", expiration=EXPIRY, validity_status="RANKABLE",
            semantic_compatibility=D("1.0"), semantic_tier="strong",
            core_median_return=D("0.09"), core_worst_return=D("-0.10"),
            core_best_return=D("0.20"), core_positive_scenario_fraction=D("0.70"),
            no_profitable_region=False, mean_relative_spread=D("0.04"),
            capital_utilisation=D("0.01"), entry_cash_required=D("0"),
        )
        db_session.add(candidate)
        db_session.flush()
        for index, (action, right, strike, bid, ask) in enumerate(
            (("sell", "put", "100", "1.00", "1.10"), ("buy", "put", "99", "0.10", "0.20"))
        ):
            db_session.add(
                V4ShadowCandidateLeg(
                    shadow_candidate_id=candidate.id, leg_index=index, action=action,
                    right=right, strike=D(strike), quantity=1, multiplier=D("100"),
                    external_contract_id=f"tiny-{index}", bid=D(bid), ask=D(ask),
                    market_data_quality="delayed",
                )
            )
        db_session.flush()

        rows = _by_key(evaluate_challenger(db_session, decision).config_rows)

        assert all(r["status"] == "RANKED" for r in rows.values())
        assert {r["selected_candidate_id"] for r in rows.values()} == {"put_credit_spread:TINY"}
