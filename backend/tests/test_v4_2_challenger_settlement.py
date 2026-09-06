"""V4.2 -- challenger SETTLEMENT and realized outcomes.

The challenger must settle on exactly the control's terms and never on kinder
ones. These tests are about parity and about the frozen position: the exact
contracts, the required executable side, the same empty-book semantics, the
same end-of-day fallback, and an append-only record where a failed attempt
survives its own recovery.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.exc import DatabaseError, IntegrityError

from analytics.decision.v4_2_viability import VIABILITY_GATE_VERSION
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
    V4ShadowDecision,
)
from providers.types import OptionQuote
from services.v4_2_challenger_entry import freeze_challenger_entries
from services.v4_2_challenger_recovery import recover_challenger_settlements
from services.v4_2_challenger_settlement import (
    EXIT_NO_ASK,
    EXIT_NO_BID,
    SETTLEMENT_STATUS_FAILED,
    SETTLEMENT_STATUS_SETTLED,
    settle_challenger_decision,
)

D = Decimal
ENTRY_AT = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)
EXIT_AT = datetime(2026, 9, 11, 19, 30, tzinfo=UTC)
EXPIRATION = date(2026, 9, 25)


class FakeProvider:
    """Answers only for the exact conIds it was given, and counts its calls.

    Deliberately NOT a generic mock: the point of several tests below is that
    settlement resolves by frozen contract identity, so a provider that
    happily answers for a strike it was never asked about would hide the very
    defect being tested for.
    """

    def __init__(self, quotes: dict[str, dict], *, expiration=EXPIRATION):
        self.quotes = quotes
        self.expiration = expiration
        self.calls: list[tuple[str, tuple[str, ...], date]] = []

    def get_quotes_for_known_contracts(self, ticker, contracts, expiration, as_of):
        self.calls.append((ticker, tuple(c.external_contract_id for c in contracts), expiration))
        out = []
        for contract in contracts:
            spec = self.quotes.get(contract.external_contract_id)
            if spec is None:
                continue
            out.append(
                OptionQuote(
                    source_provider="fake",
                    retrieved_at=EXIT_AT,
                    ticker=ticker,
                    snapshot_timestamp=EXIT_AT,
                    expiration_date=expiration,
                    strike=contract.strike,
                    option_type=contract.option_type,
                    market_data_quality="delayed",
                    external_contract_id=contract.external_contract_id,
                    **spec,
                )
            )
        return out


def _position(db, *, symbol, legs, configs=("v4_2k_moderate",), expiration=EXPIRATION):
    """A frozen challenger position ready to settle."""
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Co",
        earnings_date=date(2026, 9, 10),
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
        underlying_price=D("100"),
    )
    db.add(control)
    db.flush()
    candidate = V4ShadowCandidate(
        shadow_decision_id=control.id,
        candidate_id="pos:v1",
        strategy="bull_call_spread",
        expiration=expiration,
        validity_status="RANKABLE",
        core_median_return=D("0.05"),
        entry_cash_required=D("100"),
    )
    db.add(candidate)
    db.flush()
    for index, (action, right, strike, conid, bid, ask) in enumerate(legs):
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
                bid=D(bid),
                ask=D(ask),
                market_data_quality="delayed",
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
    freeze_challenger_entries(db, challenger=challenger, settlement_date=date(2026, 9, 11))
    return challenger


class TestExecutableExit:
    def test_a_long_leg_is_closed_at_the_bid(self, db_session):
        challenger = _position(
            db_session,
            symbol="EXITL",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        provider = FakeProvider({"111": {"bid": D("2.00"), "ask": D("2.40")}})
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        obs = db_session.query(V42ChallengerCandidateObservation).filter_by(phase="EXIT").one()
        assert obs.net_executable_value == D("200"), "closed at the BID, not the ask"
        assert obs.legs_json["legs"][0]["pricing_source"] == "EXECUTABLE_BID"

    def test_a_short_leg_is_closed_at_the_ask(self, db_session):
        challenger = _position(
            db_session,
            symbol="EXITS",
            legs=[("sell", "call", "105", "222", "0.40", "0.60")],
        )
        provider = FakeProvider({"222": {"bid": D("0.10"), "ask": D("0.30")}})
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        obs = db_session.query(V42ChallengerCandidateObservation).filter_by(phase="EXIT").one()
        # Buying back a short at the ASK is a negative net exit value.
        assert obs.net_executable_value == D("-30")
        assert obs.legs_json["legs"][0]["pricing_source"] == "EXECUTABLE_ASK"


class TestFrozenContractIdentity:
    def test_settlement_requotes_the_exact_frozen_conids(self, db_session):
        challenger = _position(
            db_session,
            symbol="FROZE",
            legs=[
                ("buy", "call", "100", "111", "1.00", "1.20"),
                ("sell", "call", "105", "222", "0.40", "0.60"),
            ],
        )
        provider = FakeProvider(
            {
                "111": {"bid": D("2.00"), "ask": D("2.20")},
                "222": {"bid": D("0.80"), "ask": D("1.00")},
            }
        )
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        assert len(provider.calls) == 1, "one quote call for the whole expiration group"
        _ticker, conids, expiration = provider.calls[0]
        assert set(conids) == {"111", "222"}
        assert expiration == EXPIRATION

    def test_no_strike_is_reselected_and_the_position_is_unchanged(self, db_session):
        challenger = _position(
            db_session,
            symbol="NORSL",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        entry_before = db_session.query(V42ChallengerConfigEntry).one()
        frozen = dict(entry_before.frozen_legs_json)
        quantity = entry_before.quantity
        provider = FakeProvider({"111": {"bid": D("2.00"), "ask": D("2.20")}})
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        settlement = db_session.query(V42ChallengerConfigSettlement).one()
        entry_after = db_session.query(V42ChallengerConfigEntry).one()
        assert entry_after.frozen_legs_json == frozen
        assert settlement.quantity == quantity
        assert settlement.candidate_id == "pos:v1"


class TestEmptyBook:
    def test_an_empty_bid_book_on_a_long_leg_is_recorded_as_no_bid(self, db_session):
        challenger = _position(
            db_session,
            symbol="NOBID",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        provider = FakeProvider(
            {"111": {"bid": None, "ask": D("0.05"), "bid_size": 0, "bid_book_empty": True}}
        )
        summary = settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        obs = db_session.query(V42ChallengerCandidateObservation).filter_by(phase="EXIT").one()
        assert obs.status == "NOT_EXECUTABLE"
        assert obs.failure_category == EXIT_NO_BID
        assert obs.legs_json["legs"][0]["required_side_state"] == "book_empty"
        assert summary.failed == 1

    def test_an_empty_ask_book_on_a_short_leg_is_recorded_as_no_ask(self, db_session):
        challenger = _position(
            db_session,
            symbol="NOASK",
            legs=[("sell", "call", "105", "222", "0.40", "0.60")],
        )
        provider = FakeProvider(
            {"222": {"bid": D("0.05"), "ask": None, "ask_size": 0, "ask_book_empty": True}}
        )
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        obs = db_session.query(V42ChallengerCandidateObservation).filter_by(phase="EXIT").one()
        assert obs.failure_category == EXIT_NO_ASK

    def test_a_silent_provider_is_not_confused_with_an_empty_book(self, db_session):
        """No tick at all is a timeout, not a market fact. Only the second is
        a real statement about the book, and only the first is worth a retry."""
        challenger = _position(
            db_session,
            symbol="SILNT",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        provider = FakeProvider({})  # answers for nothing
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        obs = db_session.query(V42ChallengerCandidateObservation).filter_by(phase="EXIT").one()
        assert obs.failure_category == "REQUIRED_SIDE_TIMEOUT"
        assert obs.legs_json["legs"][0]["required_side_state"] == "unavailable"


class TestRealizedOutcome:
    def test_realized_pnl_and_both_returns_are_persisted(self, db_session):
        challenger = _position(
            db_session,
            symbol="PNLXX",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        provider = FakeProvider({"111": {"bid": D("2.00"), "ask": D("2.20")}})
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        row = db_session.query(V42ChallengerConfigSettlement).one()
        entry = db_session.query(V42ChallengerConfigEntry).one()
        assert row.status == SETTLEMENT_STATUS_SETTLED
        # Paid the 1.20 ask, closed at the 2.00 bid: +0.80 per contract, and
        # the persisted values are that per-unit economics times this
        # configuration's own quantity.
        assert row.entry_net_value == D("120") * entry.quantity
        assert row.exit_net_value == D("200") * entry.quantity
        assert row.realized_pnl == D("80") * entry.quantity
        assert row.realized_pnl == (row.exit_net_value - row.entry_net_value)
        assert row.return_on_standardized_capital is not None
        assert row.return_on_capital_used is not None
        assert row.settlement_grade == "EXECUTABLE_BID_ASK"

    def test_settlement_quality_uses_the_controls_vocabulary(self, db_session):
        challenger = _position(
            db_session,
            symbol="GRADE",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        provider = FakeProvider({"111": {"bid": D("1.50"), "ask": D("1.70")}})
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        row = db_session.query(V42ChallengerConfigSettlement).one()
        assert row.settlement_grade in {
            "EXECUTABLE_BID_ASK",
            "MARKET_CLOSE_FALLBACK",
            "EXPIRATION_INTRINSIC_AT_CLOSE",
            "UNRESOLVED",
        }

    def test_challenger_outcomes_never_appear_in_the_control_tables(self, db_session):
        from models.v4_shadow import V4ShadowConfigSettlement  # noqa: PLC0415

        before = db_session.query(V4ShadowConfigSettlement).count()
        challenger = _position(
            db_session,
            symbol="COHRT",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        provider = FakeProvider({"111": {"bid": D("2.00"), "ask": D("2.20")}})
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        assert db_session.query(V4ShadowConfigSettlement).count() == before
        assert db_session.query(V42ChallengerConfigSettlement).count() == 1


class TestAppendOnlyHistory:
    def test_only_one_settlement_of_record_per_configuration(self, db_session):
        challenger = _position(
            db_session,
            symbol="ONEOF",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        provider = FakeProvider({"111": {"bid": D("2.00"), "ask": D("2.20")}})
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        db_session.flush()
        first = db_session.query(V42ChallengerConfigSettlement).one()
        db_session.add(
            V42ChallengerConfigSettlement(
                challenger_config_result_id=first.challenger_config_result_id,
                challenger_decision_id=first.challenger_decision_id,
                challenger_config_entry_id=first.challenger_config_entry_id,
                configuration_key=first.configuration_key,
                candidate_id=first.candidate_id,
                status="SETTLED",
                quantity=1,
                standardized_capital=D("2000"),
                settled_at=EXIT_AT,
                pricing_convention="CLOSE_LONG_AT_BID_CLOSE_SHORT_AT_ASK",
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()
        db_session.rollback()

    def test_many_failed_attempts_are_permitted(self, db_session):
        challenger = _position(
            db_session,
            symbol="MANYF",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        entry = db_session.query(V42ChallengerConfigEntry).one()
        for _ in range(3):
            db_session.add(
                V42ChallengerConfigSettlement(
                    challenger_config_result_id=entry.challenger_config_result_id,
                    challenger_decision_id=challenger.id,
                    challenger_config_entry_id=entry.id,
                    configuration_key=entry.configuration_key,
                    candidate_id=entry.candidate_id,
                    status=SETTLEMENT_STATUS_FAILED,
                    quantity=1,
                    standardized_capital=D("2000"),
                    settled_at=EXIT_AT,
                    pricing_convention="CLOSE_LONG_AT_BID_CLOSE_SHORT_AT_ASK",
                    failure_category="NO_BID",
                )
            )
        db_session.flush()
        assert db_session.query(V42ChallengerConfigSettlement).count() == 3

    def test_a_settlement_row_cannot_be_updated(self, db_session):
        challenger = _position(
            db_session,
            symbol="IMMUT",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        provider = FakeProvider({"111": {"bid": D("2.00"), "ask": D("2.20")}})
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        db_session.commit()
        row = db_session.query(V42ChallengerConfigSettlement).first()
        row.realized_pnl = D("9999")
        with pytest.raises(DatabaseError):
            db_session.flush()
        db_session.rollback()

    def test_an_already_settled_configuration_is_skipped(self, db_session):
        challenger = _position(
            db_session,
            symbol="SKIPP",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        provider = FakeProvider({"111": {"bid": D("2.00"), "ask": D("2.20")}})
        settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        again = settle_challenger_decision(
            db_session, provider=provider, challenger=challenger, observed_at=EXIT_AT
        )
        assert again.settled == 0
        assert again.skipped_already == 1
        assert db_session.query(V42ChallengerConfigSettlement).count() == 1


class TestQuoteSharingWithControl:
    def test_a_shared_contract_is_never_requoted(self, db_session):
        challenger = _position(
            db_session,
            symbol="SHARE",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        control_quote = OptionQuote(
            source_provider="ibkr_tws",
            retrieved_at=EXIT_AT,
            ticker="SHARE",
            snapshot_timestamp=EXIT_AT,
            expiration_date=EXPIRATION,
            strike=D("100"),
            option_type="call",
            bid=D("2.00"),
            ask=D("2.20"),
            market_data_quality="delayed",
            external_contract_id="111",
        )
        provider = FakeProvider({})  # would answer nothing if asked
        summary = settle_challenger_decision(
            db_session,
            provider=provider,
            challenger=challenger,
            observed_at=EXIT_AT,
            shared_quotes={"111": control_quote},
        )
        assert provider.calls == [], "the control already quoted this contract"
        assert summary.quote_calls == 0
        assert summary.contracts_reused_from_control == 1
        assert summary.settled == 1
        leg = (
            db_session.query(V42ChallengerCandidateObservation)
            .filter_by(phase="EXIT")
            .one()
            .legs_json["legs"][0]
        )
        assert leg["quote_source"] == "reused_from_control"

    def test_only_challenger_only_contracts_are_requested(self, db_session):
        challenger = _position(
            db_session,
            symbol="PARTL",
            legs=[
                ("buy", "call", "100", "111", "1.00", "1.20"),
                ("sell", "call", "105", "222", "0.40", "0.60"),
            ],
        )
        control_quote = OptionQuote(
            source_provider="ibkr_tws",
            retrieved_at=EXIT_AT,
            ticker="PARTL",
            snapshot_timestamp=EXIT_AT,
            expiration_date=EXPIRATION,
            strike=D("100"),
            option_type="call",
            bid=D("2.00"),
            ask=D("2.20"),
            market_data_quality="delayed",
            external_contract_id="111",
        )
        provider = FakeProvider({"222": {"bid": D("0.80"), "ask": D("1.00")}})
        summary = settle_challenger_decision(
            db_session,
            provider=provider,
            challenger=challenger,
            observed_at=EXIT_AT,
            shared_quotes={"111": control_quote},
        )
        assert len(provider.calls) == 1
        assert provider.calls[0][1] == ("222",), "only the contract the control did not hold"
        assert summary.contracts_reused_from_control == 1
        assert summary.settled == 1


class TestEndOfDayFallbackParity:
    """The challenger uses the control's released hierarchy, with no more
    favourable variant of its own."""

    class CloseProvider(FakeProvider):
        def __init__(self, quotes, *, closes=None, underlying=None, expiration=EXPIRATION):
            super().__init__(quotes, expiration=expiration)
            self.closes = closes or {}
            self.underlying = underlying

        def get_session_close_with_source(self, conid, session_date):
            return self.closes.get(str(conid)), "last_rth_trade_bar"

        def get_underlying_session_close(self, ticker, session_date):
            return self.underlying

    def _stranded(self, db, symbol, *, expiration=EXPIRATION):
        challenger = _position(
            db,
            symbol=symbol,
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
            expiration=expiration,
        )
        empty = FakeProvider(
            {"111": {"bid": None, "ask": D("0.05"), "bid_size": 0, "bid_book_empty": True}}
        )
        settle_challenger_decision(db, provider=empty, challenger=challenger, observed_at=EXIT_AT)
        return challenger

    def test_a_closing_mark_settles_a_stranded_position(self, db_session):
        self._stranded(db_session, "EODCM")
        provider = self.CloseProvider({}, closes={"111": D("0.55")})
        summary = recover_challenger_settlements(
            db_session,
            provider=provider,
            session_date=date(2026, 9, 11),
            now=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
            dry_run=False,
        )
        assert summary.settled == 1
        settled = db_session.query(V42ChallengerConfigSettlement).filter_by(status="SETTLED").one()
        assert settled.pricing_method == "MARKET_CLOSE_FALLBACK"
        assert settled.settlement_grade == "MARKET_CLOSE_FALLBACK"
        assert settled.recovery_provenance == "EOD_SETTLEMENT_FALLBACK"

    def test_the_original_failure_survives_its_recovery(self, db_session):
        self._stranded(db_session, "EODAP")
        failed = db_session.query(V42ChallengerConfigSettlement).one()
        failed_id, failed_detail = failed.id, failed.failure_detail
        provider = self.CloseProvider({}, closes={"111": D("0.55")})
        recover_challenger_settlements(
            db_session,
            provider=provider,
            session_date=date(2026, 9, 11),
            now=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
            dry_run=False,
        )
        original = db_session.get(V42ChallengerConfigSettlement, failed_id)
        assert original.status == SETTLEMENT_STATUS_FAILED
        assert original.failure_detail == failed_detail
        recovery = db_session.query(V42ChallengerConfigSettlement).filter_by(status="SETTLED").one()
        assert recovery.supersedes_settlement_id == failed_id

    def test_a_living_option_is_never_written_down_to_zero(self, db_session):
        """Rule 4: a non-expiring contract with an empty book and no closing
        mark stays unresolved. It is not worth zero; it is unquoted."""
        self._stranded(db_session, "EODLV")
        provider = self.CloseProvider({}, closes={}, underlying=D("90"))
        summary = recover_challenger_settlements(
            db_session,
            provider=provider,
            session_date=date(2026, 9, 11),
            now=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
            dry_run=False,
        )
        assert summary.settled == 0
        assert summary.unresolved == 1
        assert (
            db_session.query(V42ChallengerConfigSettlement).filter_by(status="SETTLED").count() == 0
        )

    def test_expiration_intrinsic_applies_only_on_the_expiry_date(self, db_session):
        self._stranded(db_session, "EODIN", expiration=date(2026, 9, 11))
        provider = self.CloseProvider(
            {}, closes={}, underlying=D("108"), expiration=date(2026, 9, 11)
        )
        summary = recover_challenger_settlements(
            db_session,
            provider=provider,
            session_date=date(2026, 9, 11),
            now=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
            dry_run=False,
        )
        assert summary.settled == 1
        settled = db_session.query(V42ChallengerConfigSettlement).filter_by(status="SETTLED").one()
        assert settled.pricing_method == "EXPIRATION_INTRINSIC_AT_CLOSE"
        # A 100 call against a 108 underlying close is worth 8.00 of intrinsic
        # per share, so 800 per contract times the configuration's own size.
        assert settled.exit_net_value == D("800") * settled.quantity

    def test_a_recovery_dry_run_writes_nothing(self, db_session):
        self._stranded(db_session, "EODDR")
        before = db_session.query(V42ChallengerConfigSettlement).count()
        provider = self.CloseProvider({}, closes={"111": D("0.55")})
        summary = recover_challenger_settlements(
            db_session,
            provider=provider,
            session_date=date(2026, 9, 11),
            now=datetime(2026, 9, 11, 20, 30, tzinfo=UTC),
            dry_run=True,
        )
        db_session.flush()
        assert summary.settled == 1
        assert db_session.query(V42ChallengerConfigSettlement).count() == before


class TestWindowDiscipline:
    def test_a_missed_window_is_closed_terminally_and_never_quoted_late(self, db_session):
        from services.v4_2_challenger_settlement import fail_missed_challenger_window

        challenger = _position(
            db_session,
            symbol="MISSW",
            legs=[("buy", "call", "100", "111", "1.00", "1.20")],
        )
        closed = fail_missed_challenger_window(
            db_session,
            challenger=challenger,
            observed_at=EXIT_AT + timedelta(hours=6),
            detail="legal settlement window had already passed",
        )
        assert closed == 1
        row = db_session.query(V42ChallengerConfigSettlement).one()
        assert row.status == SETTLEMENT_STATUS_FAILED
        assert row.failure_category == "SETTLEMENT_WINDOW_MISSED"
        assert row.exit_net_value is None
