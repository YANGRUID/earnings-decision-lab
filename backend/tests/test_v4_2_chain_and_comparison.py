"""V4.2 -- chain metadata freeze and the control/challenger read model."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from analytics.decision_timing_policy import V4_TIMING_POLICY
from models.earnings_calendar_event import EarningsCalendarEvent
from models.v4_2_challenger import V4ChainMetadataSnapshot
from models.v4_shadow import SHADOW_SCHEMA_VERSION, V4ShadowCandidate, V4ShadowDecision
from services.v4_2_chain_metadata import capture_chain_metadata
from services.v4_2_comparison import compare_event

D = Decimal
OBSERVED = datetime(2026, 9, 3, 19, 30, tzinfo=UTC)


class _FakeProvider:
    """Returns listed metadata only -- and counts calls, so a test can prove
    no quote path was touched."""

    def __init__(self, expirations=None, strikes=None, none_result=False):
        self.calls = 0
        self.quote_calls = 0
        self._expirations = expirations or [
            date(2026, 9, 4), date(2026, 9, 11), date(2026, 9, 18), date(2026, 10, 16)
        ]
        self._strikes = strikes or [D("90"), D("95"), D("100"), D("105")]
        self._none = none_result

    def get_chain_metadata(self, ticker):
        self.calls += 1
        if self._none:
            return None
        return {
            "underlying_conid": "265598",
            "trading_class": ticker,
            "exchange": "SMART",
            "multiplier": "100",
            "expirations": self._expirations,
            "strikes": self._strikes,
            "source_provider": "ibkr_tws",
        }

    def get_quotes_for_known_contracts(self, *a, **k):  # pragma: no cover
        self.quote_calls += 1
        raise AssertionError("metadata capture must never request quotes")


@pytest.fixture
def event(db_session):
    row = EarningsCalendarEvent(
        symbol="CHNX", company_name="Chain Co", earnings_date=date(2026, 9, 3),
        earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
    )
    db_session.add(row)
    db_session.flush()
    return row


def _capture(db_session, event, provider, dry_run):
    return capture_chain_metadata(
        db_session, provider=provider, ticker="CHNX",
        earnings_calendar_event_id=event.id, earnings_date=date(2026, 9, 3),
        settlement_date=date(2026, 9, 4), decision_date=date(2026, 9, 3),
        observed_at=OBSERVED, dry_run=dry_run,
    )


class TestChainMetadataCapture:
    def test_it_costs_one_metadata_request_and_no_market_data(self, db_session, event):
        provider = _FakeProvider()
        out = _capture(db_session, event, provider, dry_run=True)
        assert out.metadata_requests == 1
        assert out.market_data_requests == 0
        assert provider.quote_calls == 0

    def test_a_dry_run_writes_nothing(self, db_session, event):
        _capture(db_session, event, _FakeProvider(), dry_run=True)
        db_session.flush()
        assert db_session.query(V4ChainMetadataSnapshot).count() == 0

    def test_freezing_records_the_complete_listed_set(self, db_session, event):
        _capture(db_session, event, _FakeProvider(), dry_run=False)
        db_session.flush()
        row = db_session.query(V4ChainMetadataSnapshot).one()
        assert len(row.available_expirations) == 4
        assert len(row.listed_strikes["chain"]) == 4
        assert row.trading_class == "CHNX"
        assert row.exchange == "SMART"

    def test_the_considered_ladder_is_bounded_and_explained(self, db_session, event):
        out = _capture(db_session, event, _FakeProvider(), dry_run=True)
        assert len(out.considered) == 3, "the ladder must stay bounded"
        first = out.considered[0]
        assert first["ladder_position"] == 0
        assert first["expires_on_settlement_date"] is True
        assert first["settlement_risk"] == "EXPIRES_ON_SETTLEMENT_DATE"

    def test_it_answers_which_expirations_existed(self, db_session, event):
        """The question the historical events cannot answer."""
        _capture(db_session, event, _FakeProvider(), dry_run=False)
        db_session.flush()
        row = db_session.query(V4ChainMetadataSnapshot).one()
        assert "2026-10-16" in row.available_expirations
        assert len(row.considered_expirations) < len(row.available_expirations)

    def test_refreezing_the_same_window_is_a_no_op(self, db_session, event):
        _capture(db_session, event, _FakeProvider(), dry_run=False)
        db_session.flush()
        provider = _FakeProvider()
        again = _capture(db_session, event, provider, dry_run=False)
        db_session.flush()
        assert not again.frozen
        assert db_session.query(V4ChainMetadataSnapshot).count() == 1
        assert provider.calls == 0, "an existing snapshot must not re-request metadata"

    def test_a_frozen_snapshot_can_never_be_updated(self, db_session, event):
        _capture(db_session, event, _FakeProvider(), dry_run=False)
        db_session.flush()
        row = db_session.query(V4ChainMetadataSnapshot).one()
        row.metadata_quality = "edited"
        with pytest.raises(Exception):  # noqa: B017 -- DB trigger
            db_session.flush()

    def test_missing_metadata_is_reported_not_invented(self, db_session, event):
        out = _capture(db_session, event, _FakeProvider(none_result=True), dry_run=False)
        db_session.flush()
        assert not out.frozen
        assert out.reason and "no listed option metadata" in out.reason
        assert db_session.query(V4ChainMetadataSnapshot).count() == 0


class TestComparisonReadModel:
    @pytest.fixture
    def control(self, db_session, event):
        decision = V4ShadowDecision(
            earnings_calendar_event_id=event.id, ticker="CHNX", company_name="Chain Co",
            legal_decision_window_at=OBSERVED, generated_at=OBSERVED, as_of=OBSERVED,
            status="RANKED", engine_version="v4-test",
            shadow_schema_version=SHADOW_SCHEMA_VERSION,
            decision_timing_policy_version=V4_TIMING_POLICY.version,
            candidate_count=1, rankable_candidate_count=1,
            rank_1_candidate_id="c:v1",
        )
        db_session.add(decision)
        db_session.flush()
        db_session.add(V4ShadowCandidate(
            shadow_decision_id=decision.id, candidate_id="c:v1", strategy="long_call",
            expiration=date(2026, 9, 18), validity_status="RANKABLE",
            core_median_return=D("-0.05"),
        ))
        db_session.flush()
        return decision

    def test_an_unevaluated_challenger_is_not_reported_as_a_disagreement(
        self, db_session, control
    ):
        out = compare_event(db_session, control)
        assert out.challenger.status is None
        assert out.differs is False, "absence of an evaluation is not a difference"

    def test_the_control_side_is_reported_from_its_own_frozen_evidence(
        self, db_session, control
    ):
        out = compare_event(db_session, control)
        assert out.control.status == "RANKED"
        assert out.control.strategy == "long_call"
        assert out.control.median_return == D("-0.05")

    def test_evidence_readiness_reports_multi_expiry_honestly(self, db_session, control):
        out = compare_event(db_session, control)
        assert out.challenger_evidence["multi_expiry_replay"] == "CANNOT_REPLAY_HONESTLY"

    def test_a_frozen_chain_makes_multi_expiry_replay_available(
        self, db_session, control, event
    ):
        _capture(db_session, event, _FakeProvider(), dry_run=False)
        db_session.flush()
        out = compare_event(db_session, control)
        assert out.challenger_evidence["multi_expiry_metadata"] == "READY"
        assert out.challenger_evidence["multi_expiry_replay"] == "AVAILABLE"

    def test_neither_side_is_labelled_better(self, db_session, control):
        out = compare_event(db_session, control)
        for label in (out.control.methodology, out.challenger.methodology):
            assert "better" not in label.lower()
            assert "winner" not in label.lower()
        assert "CONTROL" in out.control.methodology
        assert "CHALLENGER" in out.challenger.methodology
