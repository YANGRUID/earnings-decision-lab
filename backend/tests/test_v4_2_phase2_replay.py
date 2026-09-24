"""V4.2 Phase 2 -- what the past can and cannot be asked.

The property that matters most here is a refusal. Phase 2's change is the
search space, and the evidence needed to replay a multi-expiry search on a
past event -- that event's listed metadata and its per-expiry quotes -- was
never frozen. Reconstructing it from today's chain would measure hindsight,
so the classifier must say CANNOT rather than produce a number.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision_timing_policy import V4_TIMING_POLICY
from models.earnings_calendar_event import EarningsCalendarEvent
from models.v4_2_challenger import V4ChainMetadataSnapshot
from models.v4_shadow import (
    SHADOW_SCHEMA_VERSION,
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowDecision,
)
from services.v4_2_phase2_replay import (
    REPLAY_FULL,
    REPLAY_NONE,
    REPLAY_PARTIAL,
    classify_replay,
    replay_configuration_policy,
    replay_report,
)

D = Decimal
OBSERVED = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)


def _event(db, symbol):
    event = EarningsCalendarEvent(
        symbol=symbol, company_name=f"{symbol} Co", earnings_date=date(2026, 9, 10),
        earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
    )
    db.add(event)
    db.flush()
    return event


def _control(db, symbol):
    event = _event(db, symbol)
    decision = V4ShadowDecision(
        earnings_calendar_event_id=event.id, ticker=symbol, company_name=f"{symbol} Co",
        legal_decision_window_at=OBSERVED, generated_at=OBSERVED, as_of=OBSERVED,
        status="RANKED", engine_version="v4-test",
        shadow_schema_version=SHADOW_SCHEMA_VERSION,
        decision_timing_policy_version=V4_TIMING_POLICY.version,
        candidate_count=0, rankable_candidate_count=0,
        underlying_price=D("100"), market_data_quality="delayed",
        expected_move={"implied_move_pct": "0.06"},
    )
    db.add(decision)
    db.flush()
    return decision


def _candidate(db, decision, cid, *, strategy="bull_call_spread", median="0.08",
               expiration=date(2026, 9, 25), priceable=True, entry_cash="400",
               validity_status="RANKABLE"):
    candidate = V4ShadowCandidate(
        shadow_decision_id=decision.id, candidate_id=cid, strategy=strategy,
        expiration=expiration, validity_status=validity_status,
        semantic_compatibility=D("0.9"), semantic_tier="strong",
        core_median_return=D(median), core_worst_return=D("-0.10"),
        core_best_return=D("0.30"), core_positive_scenario_fraction=D("0.60"),
        no_profitable_region=False, mean_relative_spread=D("0.05"),
        capital_utilisation=D("0.20"), entry_cash_required=D(entry_cash),
    )
    db.add(candidate)
    db.flush()
    for index, (action, right, strike) in enumerate(
        (("buy", "call", D("100")), ("sell", "call", D("105")))
    ):
        db.add(
            V4ShadowCandidateLeg(
                shadow_candidate_id=candidate.id, leg_index=index, action=action,
                right=right, strike=strike, quantity=1, multiplier=D("100"),
                external_contract_id=f"{cid}-{index}",
                bid=D("1.00") if priceable else None,
                ask=D("1.20") if priceable else None,
                market_data_quality="delayed",
            )
        )
    db.flush()
    return candidate


class TestClassification:
    def test_a_single_frozen_expiry_is_partial_not_full(self, db_session):
        """Every real event is this case: the selection can be replayed, the
        multi-expiry search cannot."""
        control = _control(db_session, "RPPART")
        _candidate(db_session, control, "a:v1")

        classification = classify_replay(db_session, control)

        assert classification.mode == REPLAY_PARTIAL
        assert classification.frozen_expiries == 1
        assert classification.has_chain_metadata_snapshot is False
        assert "no listed metadata was frozen" in classification.reason

    def test_a_candidate_with_no_executable_side_cannot_be_replayed(self, db_session):
        control = _control(db_session, "RPNONE")
        _candidate(db_session, control, "a:v1", priceable=False)

        classification = classify_replay(db_session, control)

        assert classification.mode == REPLAY_NONE
        assert "inventing a price" in classification.reason

    def test_frozen_metadata_and_several_expiries_are_full(self, db_session):
        control = _control(db_session, "RPFULL")
        _candidate(db_session, control, "a:v1", expiration=date(2026, 9, 25))
        _candidate(db_session, control, "b:v1", expiration=date(2026, 10, 16))
        db_session.add(
            V4ChainMetadataSnapshot(
                earnings_calendar_event_id=control.earnings_calendar_event_id,
                ticker="RPFULL", observed_at=OBSERVED,
                available_expirations=["2026-09-25", "2026-10-16"],
                listed_strikes=["100", "105"], source_provider="ibkr_tws",
                metadata_quality="listed_metadata_complete",
            )
        )
        db_session.flush()

        classification = classify_replay(db_session, control)

        assert classification.mode == REPLAY_FULL
        assert classification.frozen_expiries == 2


class TestTheConfigurationPolicyReplay:
    def test_it_re_decides_without_re_pricing(self, db_session):
        control = _control(db_session, "RPPOL")
        _candidate(db_session, control, "cheap:v1", median="0.05", entry_cash="180")
        _candidate(db_session, control, "rich:v1", median="0.20", entry_cash="2600")

        replay = replay_configuration_policy(db_session, control)

        assert replay.universe == 2
        assert len(replay.decisions) == 6

    def test_capital_incompatible_candidates_are_included(self, db_session):
        """V4.1 marks anything above the $2,000 standardized capital
        CAPITAL_INCOMPATIBLE. Excluding those from the replay would replay the
        very defect under study."""
        control = _control(db_session, "RPCAP")
        _candidate(
            db_session, control, "big:v1", entry_cash="7000",
            validity_status="CAPITAL_INCOMPATIBLE",
        )

        replay = replay_configuration_policy(db_session, control)

        assert replay.universe == 1
        assert any(
            d.diagnostics.universe_count == 1 for d in replay.decisions
        )


class TestNoOutcomeIsEverRead:
    def test_the_module_imports_no_settlement_or_outcome_model(self):
        """A replay that could see the outcome is a way to tune the engine on
        it by accident. Checked against the real import graph rather than the
        text, so a comment mentioning settlement cannot fail it and an import
        buried in a function body cannot pass."""
        import ast  # noqa: PLC0415

        import services.v4_2_phase2_replay as module  # noqa: PLC0415

        with open(module.__file__) as handle:
            tree = ast.parse(handle.read())
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        outcome_words = ("settlement", "realized", "pricereaction", "outcome", "pnl")
        offenders = [
            name
            for name in imported
            if any(word in name.lower() for word in outcome_words)
        ]
        assert not offenders, f"the replay module imports outcome evidence: {offenders}"

    def test_the_report_says_what_it_refuses_to_do(self, db_session):
        control = _control(db_session, "RPRPT")
        _candidate(db_session, control, "a:v1")

        report = replay_report(db_session, limit=10)

        assert report["mode"] == "ZERO_OUTCOME_REPLAY"
        assert "hindsight" in report["notice"]
        assert report["counts"][REPLAY_PARTIAL] >= 1
        assert all("realized" not in str(e) for e in report["events"])
