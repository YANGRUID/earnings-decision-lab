"""A thesis must be fresh AT THE LEGAL WINDOW, not merely at the moment a
preparation pass happens to look.

Measured gap (2026-09-24). The catch-up ran at 13:00 ET and judged freshness
at 13:00; the decision gate judged it again at 15:30. A thesis crossing
THESIS_FRESHNESS_DAYS between those two moments was fresh when the only pass
that could still refresh it looked, and stale when the gate looked -- the
window lost as RESEARCH_NOT_READY with nothing having failed. COST crossed at
05:31 that day and was caught only because the crossing fell before 13:00.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from models.ai_thesis_version import AIThesisVersion
from models.company import Company
from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsCalendarEventStatus, EarningsSource, EarningsTiming
from services.earnings_research_preparation import legal_decision_window, v4_research_ready
from services.research_orchestration import THESIS_FRESHNESS_DAYS

FRESH = timedelta(days=THESIS_FRESHNESS_DAYS)


def _company_with_thesis(db, ticker: str, thesis_at: datetime) -> Company:
    company = Company(ticker=ticker, name=f"{ticker} Inc")
    db.add(company)
    db.flush()
    db.add(
        AIThesisVersion(
            company_id=company.id,
            business_context="b",
            historical_earnings_pattern="h",
            guidance_trend="g",
            key_risks="k",
            market_setup="m",
            disclaimer="d",
            citations=[],
            provider="deepseek",
            model="deepseek-v4-flash",
            created_at=thesis_at,
        )
    )
    db.flush()
    return company


def _event(db, ticker: str, when: date, timing: EarningsTiming) -> EarningsCalendarEvent:
    row = EarningsCalendarEvent(
        symbol=ticker,
        company_name=f"{ticker} Inc",
        earnings_date=when,
        earnings_time=timing,
        status=EarningsCalendarEventStatus.UPCOMING,
        source=EarningsSource.EARNINGSAPI,
        market_cap=Decimal("50000000000"),
        country="US",
    )
    db.add(row)
    db.flush()
    return row


class TestFreshnessJudgedAtTheWindow:
    #: 2031-05-20 is a Tuesday; 13:00 ET is 17:00 UTC.
    NOW = datetime(2031, 5, 20, 17, 0, tzinfo=UTC)
    WINDOW = datetime(2031, 5, 20, 19, 30, tzinfo=UTC)  # 15:30 ET the same day

    def test_a_thesis_expiring_between_the_pass_and_the_window_is_not_ready(self, db_session):
        """The exact gap: fresh at 13:00, stale at 15:30. Expiry 14:00 ET."""
        expires_at = datetime(2031, 5, 20, 18, 0, tzinfo=UTC)
        _company_with_thesis(db_session, "TESTGAP", expires_at - FRESH)

        fresh_now, _ = v4_research_ready(db_session, "TESTGAP", now=self.NOW)
        assert fresh_now is True

        ready, why = v4_research_ready(db_session, "TESTGAP", now=self.NOW, as_of=self.WINDOW)
        assert ready is False
        assert "at the decision window" in why

    def test_a_thesis_outliving_the_window_needs_no_refresh(self, db_session):
        """Expiry 16:00 ET, window 15:30 ET -- no refresh solely for freshness."""
        expires_at = datetime(2031, 5, 20, 20, 0, tzinfo=UTC)
        _company_with_thesis(db_session, "TESTOUTLIVE", expires_at - FRESH)

        ready, why = v4_research_ready(db_session, "TESTOUTLIVE", now=self.NOW, as_of=self.WINDOW)
        assert ready is True and why == ""

    def test_the_gate_still_judges_at_now(self, db_session):
        """The 15:30 gate decides at 15:30, so its own question is unchanged --
        this fix must not move what the gate means."""
        _company_with_thesis(db_session, "TESTGATE", self.NOW - timedelta(hours=1))
        ready, _ = v4_research_ready(db_session, "TESTGATE", now=self.NOW)
        assert ready is True

    def test_an_already_stale_thesis_still_reads_stale_without_window_wording(self, db_session):
        _company_with_thesis(db_session, "TESTOLD", self.NOW - FRESH - timedelta(days=1))
        ready, why = v4_research_ready(db_session, "TESTOLD", now=self.NOW)
        assert ready is False
        assert "at the decision window" not in why


class TestTheWindowIsTheEventsOwn:
    """Never "today at 15:30": a BMO report decides the PREVIOUS trading day."""

    def test_amc_decides_on_the_earnings_day(self, db_session):
        event = _event(db_session, "TESTAMC", date(2031, 5, 20), EarningsTiming.AMC)
        assert legal_decision_window(event).astimezone(UTC).date() == date(2031, 5, 20)

    def test_bmo_decides_the_previous_trading_day(self, db_session):
        event = _event(db_session, "TESTBMO", date(2031, 5, 20), EarningsTiming.BMO)
        assert legal_decision_window(event).astimezone(UTC).date() == date(2031, 5, 19)

    def test_a_bmo_monday_report_decides_the_friday_before(self, db_session):
        """2031-05-19 is a Monday; the previous trading day is Friday 05-16."""
        event = _event(db_session, "TESTMON", date(2031, 5, 19), EarningsTiming.BMO)
        assert legal_decision_window(event).astimezone(UTC).date() == date(2031, 5, 16)
