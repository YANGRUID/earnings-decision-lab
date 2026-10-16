"""A recovery prices a position from ITS OWN settlement session, or not at all.

Written against two real defects found on 2026-10-16, both of which had
already corrupted or stranded live forward evidence:

  * the challenger recovery had no session scoping at all. The daily 16:30
    job swept every stranded position of any age and priced them from the
    session it happened to run in -- settling JNJ, legally due 2026-10-13,
    from the 2026-10-16 close. Three extra sessions of drift turned an
    aggregate 11,373 loss into a 3,197 gain across eleven configurations.
  * both recovery services inferred the settlement session from the failed
    attempt's own timestamp, which is when the failure was DETECTED. NKE's
    legal window was 2026-10-02; a slept Mac first detected the miss on
    2026-10-05, so every subsequent recovery looked for a close three
    sessions after the contracts had expired and found nothing, forever.

Everything here is about that one rule and the guarantees around it: the
original attempt survives, exactly one settlement counts, each configuration
gets its own size, and a position with no honest same-session evidence stays
open rather than being written down.
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from analytics.decision.v4_2_viability import VIABILITY_GATE_VERSION
from analytics.decision.v4_configurations import V4_CONFIGURATION_VERSION, get_configuration
from analytics.decision_timing_policy import V4_TIMING_POLICY
from models.earnings_calendar_event import EarningsCalendarEvent
from models.v4_2_challenger import (
    V42ChallengerCandidateObservation,
    V42ChallengerConfigEntry,
    V42ChallengerConfigResult,
    V42ChallengerConfigSettlement,
    V42ChallengerDecision,
)
from models.v4_shadow import (
    SHADOW_SCHEMA_VERSION,
    V4ShadowCandidate,
    V4ShadowCandidateLeg,
    V4ShadowConfigResult,
    V4ShadowConfigSettlement,
    V4ShadowDecision,
)
from services.v4_2_challenger_entry import freeze_challenger_entries
from services.v4_2_challenger_recovery import (
    pending_settlement_sessions,
    recover_challenger_settlements,
)
from services.v4_2_challenger_settlement import settle_challenger_decision
from services.v4_emergency_settlement import _pending_configs
from services.v4_settlement_history import effective_settlements
from services.v4_settlement_sessions import SettlementSessionResolver

D = Decimal

#: AMC on Tuesday 2026-10-13 -> the legal settlement session is Wednesday
#: 2026-10-14, by the active timing policy and nothing else.
EARNINGS_DATE = date(2026, 10, 13)
LEGAL_SESSION = date(2026, 10, 14)
NEXT_SESSION = date(2026, 10, 15)
ENTRY_AT = datetime(2026, 10, 13, 19, 30, tzinfo=UTC)
WINDOW_AT = datetime(2026, 10, 14, 19, 30, tzinfo=UTC)
#: The post-close moment of the session AFTER the legal one -- a recovery run
#: late, which is the whole point.
LATE_AT = datetime(2026, 10, 15, 20, 30, tzinfo=UTC)
EXPIRATION = date(2026, 11, 20)


class RecoveryProvider:
    """Serves a closing mark ONLY for the exact (conId, session) asked for.

    Deliberately strict in both dimensions: a provider that answered for any
    session would hide the defect these tests exist for, and one that
    answered for any conId would hide the frozen-identity rule.
    """

    def __init__(self, closes: dict[tuple[str, date], Decimal], *, underlying=None):
        self.closes = closes
        self.underlying = underlying
        self.quote_calls: list[tuple[str, ...]] = []
        self.close_calls: list[tuple[str, date]] = []

    def get_quotes_for_known_contracts(self, ticker, contracts, expiration, as_of):
        self.quote_calls.append(tuple(c.external_contract_id for c in contracts))
        return []

    def get_session_close_with_source(self, conid, session_date):
        self.close_calls.append((str(conid), session_date))
        value = self.closes.get((str(conid), session_date))
        return value, ("last_rth_30min_trade_bar" if value is not None else None)

    def get_underlying_session_close(self, ticker, session_date):
        return self.underlying


def _stranded_challenger(
    db,
    symbol,
    *,
    legs=(("sell", "put", "255", "926242070"), ("buy", "put", "250", "856266723")),
    configs=("v4_2k_moderate",),
    earnings_date=EARNINGS_DATE,
    settlement_date=LEGAL_SESSION,
    expiration=EXPIRATION,
):
    """A frozen challenger position whose legal window failed, exactly as a
    gateway outage leaves one."""
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Co",
        earnings_date=earnings_date,
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db.add(event)
    db.flush()
    control = V4ShadowDecision(
        earnings_calendar_event_id=event.id,
        ticker=symbol,
        company_name=f"{symbol} Co",
        legal_decision_window_at=ENTRY_AT,
        generated_at=ENTRY_AT,
        as_of=ENTRY_AT,
        status="RANKED",
        engine_version="v4-test",
        shadow_schema_version=SHADOW_SCHEMA_VERSION,
        decision_timing_policy_version=V4_TIMING_POLICY.version,
        candidate_count=1,
        rankable_candidate_count=1,
        underlying_price=D("250"),
    )
    db.add(control)
    db.flush()
    candidate = V4ShadowCandidate(
        shadow_decision_id=control.id,
        candidate_id="pos:v1",
        strategy="put_credit_spread",
        expiration=expiration,
        validity_status="RANKABLE",
        core_median_return=D("0.05"),
        entry_cash_required=D("100"),
    )
    db.add(candidate)
    db.flush()
    for index, (action, right, strike, conid) in enumerate(legs):
        db.add(
            V4ShadowCandidateLeg(
                shadow_candidate_id=candidate.id,
                leg_index=index,
                action=action,
                right=right,
                strike=D(strike),
                quantity=1,
                multiplier=D("100"),
                external_contract_id=conid,
                bid=D("1.00"),
                ask=D("1.20"),
                market_data_quality="live",
            )
        )
    challenger = V42ChallengerDecision(
        earnings_calendar_event_id=event.id,
        shadow_decision_id=control.id,
        ticker=symbol,
        generated_at=ENTRY_AT,
        observed_at=ENTRY_AT,
        gate_version=VIABILITY_GATE_VERSION,
        move_edge_version="test",
        status="RANKED",
        selected_candidate_id="pos:v1",
        candidates_evaluated=1,
        candidates_accepted=1,
    )
    db.add(challenger)
    db.flush()
    for key in configs:
        db.add(
            V42ChallengerConfigResult(
                challenger_decision_id=challenger.id,
                configuration_key=key,
                capital_base=D("2000"),
                risk_profile="moderate",
                max_risk_dollars=D("400"),
                status="RANKED",
                selected_candidate_id="pos:v1",
            )
        )
    db.flush()
    freeze_challenger_entries(db, challenger=challenger, settlement_date=settlement_date)
    # The legal window runs and cannot price the required side -- an empty
    # book on the short leg, which is what the outage produced.
    settle_challenger_decision(
        db,
        provider=RecoveryProvider({}),
        challenger=challenger,
        observed_at=WINDOW_AT,
    )
    return challenger


def _recover(db, provider, session, *, now=LATE_AT, dry_run=False):
    return recover_challenger_settlements(
        db, provider=provider, session_date=session, now=now, dry_run=dry_run
    )


class TestTheSettlementSessionComesFromTheCalendar:
    """Not from when the failure was noticed -- the NKE defect."""

    def test_the_legal_session_is_derived_from_the_event(self, db_session):
        challenger = _stranded_challenger(db_session, "SESS1")
        resolver = SettlementSessionResolver(db_session)
        assert resolver.for_event(challenger.earnings_calendar_event_id) == LEGAL_SESSION

    def test_a_failure_stamped_days_later_does_not_move_the_session(self, db_session):
        """NKE's shape: the window was missed on 2026-10-02 and first recorded
        on 2026-10-05, so the attempt rows carry a stamp three sessions after
        the one they belong to. The recovery must still price the legal
        session -- and these rows are immutable, so a late stamp arrives as a
        further appended attempt, never as an edit."""
        challenger = _stranded_challenger(db_session, "SESS2")
        first = db_session.query(V42ChallengerConfigSettlement).one()
        db_session.add(
            V42ChallengerConfigSettlement(
                challenger_config_result_id=first.challenger_config_result_id,
                challenger_decision_id=challenger.id,
                challenger_config_entry_id=first.challenger_config_entry_id,
                configuration_key=first.configuration_key,
                candidate_id=first.candidate_id,
                status="OBSERVATION_FAILED",
                quantity=first.quantity,
                standardized_capital=first.standardized_capital,
                capital_used=first.capital_used,
                entry_net_value=first.entry_net_value,
                entry_observed_at=first.entry_observed_at,
                settled_at=datetime(2026, 10, 19, 19, 30, tzinfo=UTC),
                pricing_convention=first.pricing_convention,
                failure_category="MARKET_DATA_UNAVAILABLE_AFTER_EOD_FALLBACK",
                supersedes_settlement_id=first.id,
            )
        )
        db_session.flush()
        assert pending_settlement_sessions(db_session) == [LEGAL_SESSION]

    def test_an_unknown_calendar_event_resolves_to_no_session(self, db_session):
        """Fail closed: without the event there is no legal session, and a
        recovery that guessed one would be inventing the settlement date."""
        resolver = SettlementSessionResolver(db_session)
        assert resolver.for_event(None) is None
        assert resolver.for_event(2_000_000_000) is None


class TestARecoveryNeverPricesTheWrongSession:
    def test_a_later_session_does_not_pick_up_an_earlier_position(self, db_session):
        """The JNJ defect, directly: the daily job runs with today's date and
        must not touch a position whose legal session was earlier."""
        _stranded_challenger(db_session, "WRONG1")
        provider = RecoveryProvider(
            {
                ("926242070", NEXT_SESSION): D("0.08"),
                ("856266723", NEXT_SESSION): D("0.00"),
            }
        )
        summary = _recover(db_session, provider, NEXT_SESSION)
        assert summary.candidates_considered == 0
        assert summary.settled == 0
        assert provider.close_calls == [], "no market data for a position not due that session"
        assert (
            db_session.query(V42ChallengerConfigSettlement).filter_by(status="SETTLED").count() == 0
        )

    def test_a_close_from_another_session_cannot_settle_the_position(self, db_session):
        """Item 49: scheduled 10-14, the only close available is 10-15."""
        _stranded_challenger(db_session, "WRONG2")
        provider = RecoveryProvider({("926242070", NEXT_SESSION): D("0.08")})
        summary = _recover(db_session, provider, LEGAL_SESSION)
        assert summary.candidates_considered == 1
        assert summary.settled == 0
        assert summary.unresolved == 1
        assert {session for _conid, session in provider.close_calls} == {LEGAL_SESSION}

    def test_the_legal_session_settles_the_position(self, db_session):
        _stranded_challenger(db_session, "WRONG3")
        provider = RecoveryProvider(
            {
                ("926242070", LEGAL_SESSION): D("6.73"),
                ("856266723", LEGAL_SESSION): D("3.55"),
            }
        )
        summary = _recover(db_session, provider, LEGAL_SESSION)
        assert summary.settled == 1
        settled = db_session.query(V42ChallengerConfigSettlement).filter_by(status="SETTLED").one()
        # sell 6.73 + buy 3.55 on one contract: -673 + 355.
        assert settled.exit_net_value == D("-318")
        assert settled.pricing_method == "MARKET_CLOSE_FALLBACK"
        assert settled.settlement_grade == "MARKET_CLOSE_FALLBACK", "a mark is not a fill"


class TestACrossSessionRecoveryNeverRequotes:
    def test_no_leg_is_requoted_when_settling_an_earlier_session(self, db_session):
        """A quote taken now is NOW's price. For a position due earlier it is
        not a late fill, and labelling it EXECUTABLE_BID/ASK would make the
        forward record claim a transaction that never existed."""
        _stranded_challenger(db_session, "NOREQ")
        provider = RecoveryProvider(
            {
                ("926242070", LEGAL_SESSION): D("6.73"),
                ("856266723", LEGAL_SESSION): D("3.55"),
            }
        )
        _recover(db_session, provider, LEGAL_SESSION)
        assert provider.quote_calls == []
        settled = db_session.query(V42ChallengerConfigSettlement).filter_by(status="SETTLED").one()
        obs = db_session.get(
            V42ChallengerCandidateObservation, settled.candidate_observation_id
        )
        assert obs.legs_json["requote"] == "requote_suppressed_cross_session"
        for leg in obs.legs_json["legs"]:
            assert leg["pricing_source"] == "MARKET_CLOSE_FALLBACK"

    def test_a_same_session_recovery_still_requotes(self, db_session):
        """The released same-day behaviour is unchanged: an executable side
        that exists after the close is still the best price for that leg."""
        _stranded_challenger(db_session, "SAMEQ")
        provider = RecoveryProvider({("926242070", LEGAL_SESSION): D("6.73")})
        _recover(
            db_session,
            provider,
            LEGAL_SESSION,
            now=datetime(2026, 10, 14, 20, 30, tzinfo=UTC),
        )
        assert provider.quote_calls, "a same-session recovery may requote"


class TestTheRecoveryPersistsItsOwnEvidence:
    def test_a_prior_failed_attempt_is_not_reused_as_the_evidence_row(self, db_session):
        """The second half of the JNJ defect: a candidate whose 16:30 attempt
        already failed had its EXIT_EOD row reused verbatim, so eleven
        settled rows pointed at an observation that said NOT_EXECUTABLE and
        held no closing marks at all."""
        challenger = _stranded_challenger(db_session, "EVID1")
        # The session's own 16:30 attempt runs and fails: no closes at all.
        _recover(
            db_session,
            RecoveryProvider({}),
            LEGAL_SESSION,
            now=datetime(2026, 10, 14, 20, 30, tzinfo=UTC),
        )
        first = (
            db_session.query(V42ChallengerCandidateObservation)
            .filter_by(challenger_decision_id=challenger.id, phase="EXIT_EOD")
            .one()
        )
        assert first.status == "NOT_EXECUTABLE"
        # A later recovery finds the real marks and must not touch that row.
        provider = RecoveryProvider(
            {
                ("926242070", LEGAL_SESSION): D("6.73"),
                ("856266723", LEGAL_SESSION): D("3.55"),
            }
        )
        summary = _recover(db_session, provider, LEGAL_SESSION)
        assert summary.settled == 1
        db_session.refresh(first)
        assert first.status == "NOT_EXECUTABLE", "the failed attempt is immutable"
        assert first.net_executable_value is None
        late = (
            db_session.query(V42ChallengerCandidateObservation)
            .filter_by(challenger_decision_id=challenger.id, phase="EXIT_LATE_RECOVERY")
            .one()
        )
        assert late.status == "OBSERVED"
        assert late.net_executable_value == D("-318")
        assert late.legs_json["session_date"] == LEGAL_SESSION.isoformat()
        assert late.legs_json["recovered_on_session"] == "2026-10-15"
        settled = db_session.query(V42ChallengerConfigSettlement).filter_by(status="SETTLED").one()
        assert settled.candidate_observation_id == late.id

    def test_a_third_attempt_is_refused_rather_than_settled_unauditably(self, db_session):
        """Both evidence phases used means the provenance has nowhere to go.
        The position stays open; it is not settled off the record."""
        _stranded_challenger(db_session, "EVID2")
        for _ in range(2):
            _recover(db_session, RecoveryProvider({}), LEGAL_SESSION)
        before = db_session.query(V42ChallengerConfigSettlement).count()
        provider = RecoveryProvider(
            {
                ("926242070", LEGAL_SESSION): D("6.73"),
                ("856266723", LEGAL_SESSION): D("3.55"),
            }
        )
        summary = _recover(db_session, provider, LEGAL_SESSION)
        assert summary.skipped_exhausted == 1
        assert summary.settled == 0
        assert provider.close_calls == [], "an exhausted candidate costs no market data"
        assert db_session.query(V42ChallengerConfigSettlement).count() == before


class TestFrozenContractIdentity:
    def test_a_different_contract_at_the_same_strike_cannot_price_the_leg(self, db_session):
        """Item 50: the frozen conId is the contract. A same-strike, same-
        expiry sibling is a different instrument and never a substitute."""
        _stranded_challenger(db_session, "FROZEN")
        provider = RecoveryProvider(
            {
                ("926242071", LEGAL_SESSION): D("6.73"),
                ("856266723", LEGAL_SESSION): D("3.55"),
            }
        )
        summary = _recover(db_session, provider, LEGAL_SESSION)
        assert summary.settled == 0
        assert summary.unresolved == 1
        assert ("926242070", LEGAL_SESSION) in provider.close_calls


class TestOneSettlementOfRecord:
    def test_a_failed_attempt_and_its_recovery_count_once(self, db_session):
        """Item 51: both rows survive, one P&L observation exists."""
        _stranded_challenger(db_session, "ONCE")
        provider = RecoveryProvider(
            {
                ("926242070", LEGAL_SESSION): D("6.73"),
                ("856266723", LEGAL_SESSION): D("3.55"),
            }
        )
        _recover(db_session, provider, LEGAL_SESSION)
        rows = db_session.query(V42ChallengerConfigSettlement).all()
        assert len(rows) == 2, "the failed attempt is kept"
        effective = [
            row
            for row in rows
            if row.id
            == max(
                r.id
                for r in rows
                if r.challenger_config_result_id == row.challenger_config_result_id
            )
        ]
        assert len(effective) == 1
        assert effective[0].status == "SETTLED"
        assert sum(1 for r in rows if r.realized_pnl is not None) == 1

    def test_a_repeat_recovery_is_a_no_op(self, db_session):
        _stranded_challenger(db_session, "IDEMP")
        closes = {
            ("926242070", LEGAL_SESSION): D("6.73"),
            ("856266723", LEGAL_SESSION): D("3.55"),
        }
        _recover(db_session, RecoveryProvider(closes), LEGAL_SESSION)
        after_first = db_session.query(V42ChallengerConfigSettlement).count()
        summary = _recover(db_session, RecoveryProvider(closes), LEGAL_SESSION)
        assert summary.candidates_considered == 0
        assert db_session.query(V42ChallengerConfigSettlement).count() == after_first


class TestEachConfigurationKeepsItsOwnSize:
    def test_one_closing_mark_produces_independent_per_config_pnl(self, db_session):
        """Item 52: six configurations share a candidate and a closing
        observation, and each realizes its own frozen quantity."""
        _stranded_challenger(
            db_session,
            "SIZES",
            configs=("v4_2k_moderate", "v4_2k_aggressive", "v4_10k_aggressive"),
        )
        provider = RecoveryProvider(
            {
                ("926242070", LEGAL_SESSION): D("6.73"),
                ("856266723", LEGAL_SESSION): D("3.55"),
            }
        )
        summary = _recover(db_session, provider, LEGAL_SESSION)
        assert summary.settled == 3
        assert summary.unique_contracts == 2, "one lookup per contract, not per configuration"
        settled = db_session.query(V42ChallengerConfigSettlement).filter_by(status="SETTLED").all()
        entries = {
            e.challenger_config_result_id: e
            for e in db_session.query(V42ChallengerConfigEntry).all()
        }
        assert len({s.quantity for s in settled}) > 1, "the sizes genuinely differ"
        for row in settled:
            entry = entries[row.challenger_config_result_id]
            assert row.quantity == entry.quantity
            assert row.exit_net_value == D("-318") * entry.quantity
            assert row.realized_pnl == row.exit_net_value - entry.entry_net_value


class TestAPositionWithNoHonestEvidenceStaysOpen:
    def test_a_non_expiring_leg_with_no_closing_mark_is_not_written_to_zero(self, db_session):
        """Item 53. The brief's own words: not zero, unresolved."""
        _stranded_challenger(db_session, "OPEN1")
        provider = RecoveryProvider(
            {("926242070", LEGAL_SESSION): D("6.73")}, underlying=D("250")
        )
        summary = _recover(db_session, provider, LEGAL_SESSION)
        assert summary.settled == 0
        assert summary.unresolved == 1
        failed = (
            db_session.query(V42ChallengerConfigSettlement)
            .order_by(V42ChallengerConfigSettlement.id.desc())
            .first()
        )
        assert failed.status != "SETTLED"
        assert failed.exit_net_value is None, "no price at all, rather than a zero"

    def test_an_expired_contract_with_no_marks_is_not_settled_from_the_underlying(
        self, db_session
    ):
        """Expiration intrinsic is permitted only when the contract expires ON
        the settlement session. One that expired earlier -- PEP's shape, whose
        10-09 contracts no longer serve their own 10-08 session -- has no
        released rule, and inventing one is not recovery."""
        _stranded_challenger(db_session, "OPEN2", expiration=date(2026, 10, 9))
        provider = RecoveryProvider({}, underlying=D("128.66"))
        summary = _recover(db_session, provider, LEGAL_SESSION)
        assert summary.settled == 0
        assert summary.unresolved == 1


class TestTheControlIsScopedTheSameWay:
    def test_pending_configs_matches_the_legal_session_not_the_failure_stamp(self, db_session):
        event = EarningsCalendarEvent(
            symbol="CTRL",
            company_name="Control Co",
            earnings_date=EARNINGS_DATE,
            earnings_time="AMC",
            source="EARNINGSAPI",
            status="UPCOMING",
        )
        db_session.add(event)
        db_session.flush()
        decision = V4ShadowDecision(
            earnings_calendar_event_id=event.id,
            ticker="CTRL",
            company_name="Control Co",
            legal_decision_window_at=ENTRY_AT,
            generated_at=ENTRY_AT,
            as_of=ENTRY_AT,
            status="RANKED",
            engine_version="v4-test",
            shadow_schema_version=SHADOW_SCHEMA_VERSION,
            decision_timing_policy_version=V4_TIMING_POLICY.version,
        )
        db_session.add(decision)
        db_session.flush()
        config = get_configuration("v4_2k_moderate")
        result = V4ShadowConfigResult(
            shadow_decision_id=decision.id,
            configuration_key=config.key,
            capital_base=config.capital_base,
            risk_profile=config.risk_profile.value,
            configuration_version=V4_CONFIGURATION_VERSION,
            max_risk_dollars=config.max_risk_dollars,
            max_risk_utilization_pct=config.max_risk_utilization_pct,
            status="RANKED",
            eligible_candidate_count=1,
            excluded_candidate_count=0,
        )
        db_session.add(result)
        db_session.flush()
        db_session.add(
            V4ShadowConfigSettlement(
                shadow_config_result_id=result.id,
                shadow_decision_id=decision.id,
                configuration_key="v4_2k_moderate",
                candidate_id="pos:v1",
                status="OBSERVATION_FAILED",
                quantity=1,
                standardized_capital=D("2000"),
                capital_used=D("0"),
                entry_net_value=D("-70"),
                entry_observed_at=ENTRY_AT,
                # Detected three sessions late, exactly as NKE's was.
                settled_at=datetime(2026, 10, 19, 19, 30, tzinfo=UTC),
                pricing_convention="V4_EXIT_EXECUTABLE",
                failure_category="SETTLEMENT_WINDOW_MISSED",
            )
        )
        db_session.flush()
        resolver = SettlementSessionResolver(db_session)
        assert len(_pending_configs(db_session, LEGAL_SESSION, resolver)) == 1
        assert _pending_configs(db_session, date(2026, 10, 19), resolver) == []


class TestEffectiveSettlementsStillCountOnce:
    @pytest.mark.parametrize("statuses", [("OBSERVATION_FAILED", "SETTLED")])
    def test_the_recovered_row_is_the_one_of_record(self, statuses):
        from types import SimpleNamespace

        rows = [
            SimpleNamespace(id=index + 1, shadow_config_result_id=7, status=status)
            for index, status in enumerate(statuses)
        ]
        out = effective_settlements(rows)
        assert len(out) == 1
        assert out[0].status == "SETTLED"
