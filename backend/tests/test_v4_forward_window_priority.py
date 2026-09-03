"""Settlement priority in the 15:30 ET forward window (v4.0.0 hardening).

Deterministic, no live socket, no real sleep: the "80-second DeepSeek call"
is a fake view generator that advances a fake clock by 80 s (and, in the
threaded test, blocks on an Event while the lock is probed from outside).
"""

import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
import test_v4_5_shadow_orchestration as orch
import test_v4_six_cohort_evidence as cohort

from core.config import get_settings
from models.ai_thesis_version import AIThesisVersion
from models.company import Company
from models.earnings_calendar_event import EarningsCalendarEvent
from models.v4_shadow import (
    V4ForwardWindowTelemetry,
    V4ShadowConfigSettlement,
    V4ShadowDecision,
)
from services.v4_shadow_cohort import SETTLEMENT_WINDOW_MISSED as WINDOW_MISSED_CATEGORY
from services.v4_shadow_scheduler import (
    V4_MARKET_DATA_LOCK,
    run_forward_window,
    settle_due_cohorts,
)

ET = ZoneInfo("America/New_York")
WINDOW = datetime(2026, 9, 11, 15, 30, tzinfo=ET)  # T+1 15:30 ET for a Sep 10 AMC entry
DECISION_SYMBOLS = ("CPRT", "DOCU", "GWRE", "IOT", "ZS")
# Captured once: per-position wrappers must call the ORIGINAL, never each other.
_REAL_GENERATE = cohort.generate_shadow_decision


class FakeClock:
    """A clock the fakes can advance; nothing in these tests sleeps."""

    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class QuoteProvider:
    """Settlement quotes for the six-cohort fixture's frozen contracts.
    ``fail_for`` makes one ticker's quote call raise (failure isolation)."""

    PRICES = {
        "c100": ("6.00", "6.20"),
        "c105": ("1.50", "1.60"),
        "p95": ("9.00", "9.20"),
        "c110w": ("41.00", "41.20"),
        "c160": ("5.50", "5.60"),
    }

    def __init__(
        self, clock: FakeClock, *, fail_for: set[str] | None = None, latency_s: float = 1.0
    ):
        self.clock = clock
        self.fail_for = fail_for or set()
        self.latency_s = latency_s
        self.calls: list[tuple[str, datetime, bool]] = []

    def get_quotes_for_known_contracts(self, ticker, contracts, expiration, observed_at):
        self.calls.append((ticker, self.clock(), V4_MARKET_DATA_LOCK.locked()))
        if ticker in self.fail_for:
            raise TimeoutError(f"simulated provider timeout for {ticker}")
        self.clock.advance(self.latency_s)
        return [
            SimpleNamespace(
                strike=c.strike,
                option_type=c.option_type,
                bid=Decimal(self.PRICES[c.external_contract_id][0]),
                ask=Decimal(self.PRICES[c.external_contract_id][1]),
                market_data_quality="delayed",
                retrieved_at=observed_at,
            )
            for c in contracts
        ]


def _held_position(db, monkeypatch, symbol: str, earnings_date=date(2026, 9, 10)):
    """A frozen six-cohort decision with OBSERVED entries: settles at 15:30 ET on
    the first post-earnings trading day."""
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Held Co",
        earnings_date=earnings_date,
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db.add(event)
    db.flush()

    # The six-cohort fixture freezes under a fixed ticker; freeze this one under
    # the event's own symbol so telemetry and provider calls name it honestly.
    def _under_symbol(db_, **kwargs):
        kwargs["ticker"] = symbol
        kwargs["company_name"] = f"{symbol} Held Co"
        return _REAL_GENERATE(db_, **kwargs)

    import services.v4_shadow as shadow

    real_evaluate = shadow.evaluate_shadow_candidate
    monkeypatch.setattr(cohort, "generate_shadow_decision", _under_symbol)
    result = cohort._freeze(db, event, monkeypatch=monkeypatch)
    # _freeze patches the ranker's evaluator for ITS candidate ids; new decisions in
    # the same test must rank real candidates, so put the real evaluator back.
    monkeypatch.setattr(shadow, "evaluate_shadow_candidate", real_evaluate)
    decision = db.get(V4ShadowDecision, result.decision_id)
    assert decision.ticker == symbol  # evidence is immutable; it was frozen this way
    return event, decision


def _decision_ready_event(db, symbol: str, earnings_date=date(2026, 9, 11)):
    company = Company(ticker=symbol, name=f"{symbol} Inc")
    db.add(company)
    db.flush()
    db.add(
        AIThesisVersion(
            company_id=company.id,
            business_context="ctx",
            historical_earnings_pattern="pattern",
            guidance_trend="trend",
            key_risks="risks",
            market_setup="setup",
            disclaimer="d",
            citations=[],
            provider="deepseek",
            model="deepseek-v4-pro",
        )
    )
    event = EarningsCalendarEvent(
        symbol=symbol,
        company_name=f"{symbol} Inc",
        earnings_date=earnings_date,
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db.add(event)
    db.flush()
    return event


class SlowView:
    """The DecisionView call: 80 fake seconds, and it must never see the lock held."""

    def __init__(
        self, clock: FakeClock, seconds: float = 80.0, gate: threading.Event | None = None
    ):
        self.clock = clock
        self.seconds = seconds
        self.gate = gate
        self.calls: list[tuple[str, datetime, bool]] = []

    def __call__(self, db, company, event, now):
        self.calls.append((event.symbol, self.clock(), V4_MARKET_DATA_LOCK.locked()))
        if self.gate is not None:
            self.gate.wait(timeout=10)
        self.clock.advance(self.seconds)
        return orch._view()


class SweepAssembly:
    """The TWS chain sweep: must always run WITH the lock held."""

    def __init__(self, clock: FakeClock, seconds: float = 5.0):
        self.clock = clock
        self.seconds = seconds
        self.calls: list[tuple[str, datetime, bool]] = []

    def __call__(self, *, provider, ticker, as_of, direction, volatility_view, earnings_date):
        self.calls.append((ticker, self.clock(), V4_MARKET_DATA_LOCK.locked()))
        self.clock.advance(self.seconds)
        legs = [orch._leg(0, "buy", "call", "100"), orch._leg(1, "buy", "put", "100")]
        return orch._assembly([orch._candidate("straddle", legs)])


def _run(db, monkeypatch, *, clock, provider, view, events, now=None):
    assembly = SweepAssembly(clock)
    monkeypatch.setattr("services.v4_shadow_orchestration.assemble_shadow_candidates", assembly)
    summary = run_forward_window(
        db,
        get_settings(),
        provider=provider,
        now=now or clock(),
        clock=clock,
        view_generator=view,
        candidate_events=events,
        deadline=None,
        scheduled_at=WINDOW,
        job_started_at=clock(),
    )
    return summary, assembly


@pytest.fixture(autouse=True)
def _lock_is_free():
    assert not V4_MARKET_DATA_LOCK.locked(), "test started with the lock held"
    yield
    assert not V4_MARKET_DATA_LOCK.locked(), "test left the lock held"


class TestSameTime:
    def test_avgo_settlement_is_not_queued_behind_an_80_second_decision_view(
        self, db_session, monkeypatch
    ):
        clock = FakeClock(WINDOW)
        _, held = _held_position(db_session, monkeypatch, "AVGO")
        events = [_decision_ready_event(db_session, s) for s in DECISION_SYMBOLS]
        provider = QuoteProvider(clock)
        view = SlowView(clock, 80.0)

        summary, assembly = _run(
            db_session, monkeypatch, clock=clock, provider=provider, view=view, events=events
        )

        # Settlement acquired market data and completed before ANY DecisionView call.
        assert summary.settlement.settled > 0 and summary.settlement.failed == 0
        settle_done = max(t.completed_at for t in summary.telemetry if t.symbol == "AVGO")
        assert view.calls and settle_done <= min(at for _, at, _ in view.calls)
        assert provider.calls[0][1] == WINDOW  # first contract requested at the window instant
        # All five new decisions still ran, each with its own 80 s view.
        assert summary.decisions.ranked == len(DECISION_SYMBOLS)
        assert [s for s, _, _ in view.calls] == list(DECISION_SYMBOLS)
        # Lock scope: never held during a DecisionView, always held during a sweep.
        assert all(not held_lock for _, _, held_lock in view.calls)
        assert all(held_lock for _, _, held_lock in assembly.calls)
        assert all(held_lock for _, _, held_lock in provider.calls)
        # The settlement's own telemetry is complete.
        t = next(t for t in summary.telemetry if t.symbol == "AVGO")
        assert t.outcome == "settled"
        assert t.market_data_requested_at <= t.market_data_acquired_at
        assert t.first_contract_request_at is not None and t.required_side_ready_at is not None
        assert t.lock_wait_ms == 0 and t.total_ms is not None

    def test_a_blocked_decision_view_never_holds_the_lock(self, db_session, monkeypatch):
        """The threaded proof: while a DecisionView is in flight in the decision
        phase, another thread can take the market-data lock immediately."""
        clock = FakeClock(WINDOW)
        events = [_decision_ready_event(db_session, "CPRT")]
        gate = threading.Event()
        view = SlowView(clock, 80.0, gate=gate)
        assembly = SweepAssembly(clock)
        monkeypatch.setattr("services.v4_shadow_orchestration.assemble_shadow_candidates", assembly)
        started = threading.Event()

        def _decisions():
            from services.v4_shadow_orchestration import run_shadow_decisions_for_due_events
            from services.v4_shadow_scheduler import due_for_v4_decision_now

            started.set()
            run_shadow_decisions_for_due_events(
                db_session,
                get_settings(),
                now=WINDOW,
                provider=QuoteProvider(clock),
                view_generator=view,
                due_predicate=due_for_v4_decision_now,
                candidate_events=events,
                clock=clock,
                market_data_lock=V4_MARKET_DATA_LOCK,
            )

        worker = threading.Thread(target=_decisions, daemon=True)
        worker.start()
        started.wait(timeout=5)
        # Wait until the view call is actually in progress (it blocks on the gate).
        deadline = datetime.now(UTC) + timedelta(seconds=5)
        while not view.calls and datetime.now(UTC) < deadline:
            pass
        assert view.calls, "the decision phase never reached the view"
        acquired = V4_MARKET_DATA_LOCK.acquire(timeout=1)
        try:
            assert acquired, "a settlement could not take the lock during a DecisionView"
        finally:
            if acquired:
                V4_MARKET_DATA_LOCK.release()
        gate.set()
        worker.join(timeout=10)
        assert not worker.is_alive()


class TestMultipleSettlements:
    def test_three_due_settlements_all_precede_five_new_decisions(self, db_session, monkeypatch):
        clock = FakeClock(WINDOW)
        for sym in ("AVGO", "HPE", "SNOW"):
            _held_position(db_session, monkeypatch, sym)
        events = [_decision_ready_event(db_session, s) for s in DECISION_SYMBOLS]
        provider = QuoteProvider(clock)
        view = SlowView(clock, 80.0)

        summary, _ = _run(
            db_session, monkeypatch, clock=clock, provider=provider, view=view, events=events
        )

        assert summary.settlement.evaluated == 3 and summary.settlement.failed == 0
        settled_symbols = [t.symbol for t in summary.telemetry if t.outcome == "settled"]
        assert sorted(settled_symbols) == ["AVGO", "HPE", "SNOW"]
        last_settlement = max(t.completed_at for t in summary.telemetry)
        assert last_settlement <= min(at for _, at, _ in view.calls)
        assert summary.decisions.ranked == 5
        assert summary.settled_during_decisions == 0


class TestFailureIsolation:
    def test_one_failed_settlement_blocks_neither_other_settlements_nor_decisions(
        self, db_session, monkeypatch
    ):
        clock = FakeClock(WINDOW)
        for sym in ("AVGO", "HPE", "SNOW"):
            _held_position(db_session, monkeypatch, sym)
        events = [_decision_ready_event(db_session, s) for s in DECISION_SYMBOLS[:2]]
        provider = QuoteProvider(clock, fail_for={"HPE"})
        view = SlowView(clock, 1.0)

        summary, _ = _run(
            db_session, monkeypatch, clock=clock, provider=provider, view=view, events=events
        )

        by_symbol = {t.symbol: t for t in summary.telemetry}
        assert by_symbol["AVGO"].outcome == "settled"
        assert by_symbol["SNOW"].outcome == "settled"
        assert by_symbol["HPE"].outcome in ("failed", "partially_failed")
        assert summary.settlement.settled > 0 and summary.settlement.failed > 0
        assert summary.decisions.ranked == 2
        assert not V4_MARKET_DATA_LOCK.locked()


class TestLockCleanup:
    @pytest.mark.parametrize("error", [TimeoutError("tws timeout"), RuntimeError("provider down")])
    def test_lock_is_released_after_a_provider_failure(self, db_session, monkeypatch, error):
        clock = FakeClock(WINDOW)
        _held_position(db_session, monkeypatch, "AVGO")

        class Boom:
            def get_quotes_for_known_contracts(self, *a, **k):
                assert V4_MARKET_DATA_LOCK.locked()
                raise error

        summary = settle_due_cohorts(db_session, provider=Boom(), now=WINDOW, clock=clock)
        assert summary.failed > 0
        assert not V4_MARKET_DATA_LOCK.locked()
        assert V4_MARKET_DATA_LOCK.acquire(blocking=False)
        V4_MARKET_DATA_LOCK.release()

    def test_lock_is_released_when_the_cohort_service_itself_raises(self, db_session, monkeypatch):
        clock = FakeClock(WINDOW)
        _held_position(db_session, monkeypatch, "AVGO")
        with patch(
            "services.v4_shadow_cohort.settle_shadow_decision_cohorts",
            side_effect=RuntimeError("unexpected"),
        ):
            summary = settle_due_cohorts(
                db_session, provider=QuoteProvider(clock), now=WINDOW, clock=clock
            )
        assert summary.failed == 1
        assert not V4_MARKET_DATA_LOCK.locked()


class TestWindowMissed:
    def test_a_position_past_its_window_is_closed_without_a_late_quote(
        self, db_session, monkeypatch
    ):
        late = WINDOW + timedelta(minutes=6)
        clock = FakeClock(late)
        _held_position(db_session, monkeypatch, "AVGO")
        provider = QuoteProvider(clock)
        summary = settle_due_cohorts(db_session, provider=provider, now=late, clock=clock)
        assert summary.window_missed == 1 and provider.calls == []
        rows = db_session.query(V4ShadowConfigSettlement).all()
        assert rows and all(r.failure_category == WINDOW_MISSED_CATEGORY for r in rows)

    def test_a_window_that_closes_while_waiting_for_the_lock_is_not_quoted_late(
        self, db_session, monkeypatch
    ):
        clock = FakeClock(WINDOW + timedelta(minutes=4, seconds=59))
        _, decision = _held_position(db_session, monkeypatch, "AVGO")
        provider = QuoteProvider(clock)

        class SlowLock:
            def __enter__(self):
                clock.advance(120)  # the lock became free two minutes later
                return self

            def __exit__(self, *a):
                return False

        summary = settle_due_cohorts(
            db_session, provider=provider, now=clock(), clock=clock, market_data_lock=SlowLock()
        )
        assert summary.window_missed == 1 and provider.calls == []
        rows = (
            db_session.query(V4ShadowConfigSettlement)
            .filter_by(shadow_decision_id=decision.id)
            .all()
        )
        assert rows and all(r.failure_category == WINDOW_MISSED_CATEGORY for r in rows)


class TestIdempotency:
    def test_running_the_window_twice_settles_and_decides_exactly_once(
        self, db_session, monkeypatch
    ):
        clock = FakeClock(WINDOW)
        _held_position(db_session, monkeypatch, "AVGO")
        events = [_decision_ready_event(db_session, s) for s in DECISION_SYMBOLS[:2]]
        provider = QuoteProvider(clock)
        view = SlowView(clock, 1.0)

        first, _ = _run(
            db_session,
            monkeypatch,
            clock=clock,
            provider=provider,
            view=view,
            events=events,
            now=WINDOW,
        )
        settlements_after_first = db_session.query(V4ShadowConfigSettlement).count()
        decisions_after_first = db_session.query(V4ShadowDecision).count()

        second, _ = _run(
            db_session,
            monkeypatch,
            clock=clock,
            provider=provider,
            view=view,
            events=events,
            now=WINDOW,
        )
        assert first.settlement.settled > 0 and second.settlement.evaluated == 0
        assert second.decisions.already_generated == 2 and second.decisions.ranked == 0
        assert db_session.query(V4ShadowConfigSettlement).count() == settlements_after_first
        assert db_session.query(V4ShadowDecision).count() == decisions_after_first
        assert len(provider.calls) == 1  # settlement quotes acquired once, never again


class TestTelemetryPersistence:
    def test_the_job_persists_settlement_and_phase_telemetry(self, db_session, monkeypatch):
        """The scheduler job itself, on the rebound (test) session factory."""
        import services.v4_shadow_scheduler as jobs

        clock = FakeClock(WINDOW)
        _held_position(db_session, monkeypatch, "AVGO")
        db_session.commit()
        provider = QuoteProvider(clock)
        monkeypatch.setattr("providers.factory.get_options_provider", lambda *a, **k: provider)
        monkeypatch.setattr(
            "services.v4_shadow_orchestration.assemble_shadow_candidates", SweepAssembly(clock)
        )
        monkeypatch.setattr(jobs, "SessionLocal", lambda: db_session)
        settings = get_settings().model_copy(update={"v4_shadow_enabled": True})
        monkeypatch.setattr(jobs, "get_settings", lambda: settings)
        monkeypatch.setattr(db_session, "close", lambda: None)

        jobs.run_v4_forward_window_job(now=WINDOW, clock=clock)

        rows = db_session.query(V4ForwardWindowTelemetry).all()
        phases = {(r.phase, r.shadow_decision_id is None) for r in rows}
        assert ("settlement", False) in phases  # the AVGO position
        assert ("settlement", True) in phases and ("decision", True) in phases  # phase summaries
        avgo = next(r for r in rows if r.symbol == "AVGO")
        assert avgo.outcome == "settled" and avgo.scheduled_at is not None
        assert avgo.scheduler_run_id is not None
