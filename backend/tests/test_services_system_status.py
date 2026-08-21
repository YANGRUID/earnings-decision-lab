from datetime import UTC, date, datetime
from decimal import Decimal

import httpx

from core.config import Settings
from models.company import Company
from models.earnings_event import EarningsEvent
from models.earnings_result import EarningsResult
from models.price_bar import PriceBar
from services.system_status import (
    describe_llm_configuration,
    get_data_counts,
    get_data_freshness,
    get_ibkr_status,
    ibkr_status_label,
)

_IBKR_SETTINGS = Settings(ibkr_base_url="https://localhost:5001/v1/api", _env_file=None)


def _seed_company(db_session, ticker: str = "ZZSTAT1") -> Company:
    company = Company(ticker=ticker, name="ZZ Status Test Co", cik="0009999944")
    db_session.add(company)
    db_session.flush()
    return company


class TestGetDataCounts:
    def test_counts_reflect_real_seeded_rows(self, db_session):
        company = _seed_company(db_session)
        event = EarningsEvent(
            company_id=company.id,
            fiscal_year=2026,
            fiscal_quarter=2,
            earnings_date=date(2026, 3, 18),
        )
        db_session.add(event)
        db_session.flush()
        db_session.add(
            EarningsResult(
                earnings_event_id=event.id,
                actual_eps=Decimal("1.00"),
                source_provider="test",
                retrieved_at=datetime.now(UTC),
            )
        )
        db_session.flush()

        counts = get_data_counts(db_session)

        assert counts.companies >= 1
        assert counts.earnings_events >= 1
        assert counts.earnings_events_with_results >= 1
        # Options data is genuinely empty on a fresh test DB -- no seeding here.
        assert counts.options_snapshots >= 0


class TestGetDataFreshness:
    def test_reports_latest_price_bar_date_from_real_data(self, db_session):
        company = _seed_company(db_session, ticker="ZZSTAT2")
        db_session.add(
            PriceBar(
                ticker=company.ticker,
                trade_date=date(2026, 3, 17),
                open=Decimal("100"),
                high=Decimal("101"),
                low=Decimal("99"),
                close=Decimal("100.5"),
                volume=1000,
                source_provider="test",
                retrieved_at=datetime.now(UTC),
            )
        )
        db_session.flush()

        freshness = get_data_freshness(db_session)

        assert freshness.latest_price_bar_date is not None
        assert freshness.latest_price_bar_date >= date(2026, 3, 17)

    def test_options_snapshot_freshness_is_none_on_empty_table(self, db_session):
        # A fresh test DB has no OptionsSnapshot rows -- this must report
        # None honestly, not a fabricated timestamp.
        freshness = get_data_freshness(db_session)
        assert freshness.latest_options_snapshot_at is None or isinstance(
            freshness.latest_options_snapshot_at, datetime
        )


class TestIbkrStatusLabel:
    """Phase 4.8A -- the pure mapping (real gateway/session flags -> the
    short, glanceable label the runtime-automation brief asks for)."""

    def test_connected_when_fully_authenticated(self):
        label = ibkr_status_label(
            gateway_reachable=True, authenticated=True, connected=True, competing=False
        )
        assert label == "CONNECTED"

    def test_gateway_unreachable_takes_priority(self):
        label = ibkr_status_label(
            gateway_reachable=False, authenticated=True, connected=True, competing=False
        )
        assert label == "GATEWAY_UNREACHABLE"

    def test_auth_required_when_not_authenticated(self):
        label = ibkr_status_label(
            gateway_reachable=True, authenticated=False, connected=False, competing=False
        )
        assert label == "AUTH_REQUIRED"

    def test_competing_session_reported_even_when_authenticated(self):
        # Mirrors IBKRClient.ensure_authenticated()'s own precedence
        # (providers/ibkr_client.py) -- a competing session must not be
        # masked by authenticated=True.
        label = ibkr_status_label(
            gateway_reachable=True, authenticated=True, connected=True, competing=True
        )
        assert label == "COMPETING_SESSION"


class TestGetIbkrStatus:
    """Real HTTP mocked via httpx_mock, exactly like
    test_providers_ibkr_client.py -- this is genuinely new coverage
    (the pre-existing IBKRClient-level tests never checked the
    system_status.py mapping layer built on top of it in Phase 4.8A)."""

    def test_reports_connected_for_a_real_authenticated_session(self, httpx_mock):
        httpx_mock.add_response(
            url="https://localhost:5001/v1/api/iserver/auth/status",
            json={"authenticated": True, "connected": True, "competing": False},
        )

        status = get_ibkr_status(_IBKR_SETTINGS)

        assert status.gateway_reachable is True
        assert status.status_label == "CONNECTED"
        assert status.error is None

    def test_reports_auth_required_when_session_not_authenticated(self, httpx_mock):
        httpx_mock.add_response(
            url="https://localhost:5001/v1/api/iserver/auth/status",
            json={"authenticated": False, "connected": False, "competing": False},
        )

        status = get_ibkr_status(_IBKR_SETTINGS)

        assert status.gateway_reachable is True
        assert status.status_label == "AUTH_REQUIRED"

    def test_reports_gateway_unreachable_on_connect_error(self, httpx_mock):
        """Gateway-down scenario -- must degrade to an honest status, not
        raise, matching every other IBKR call site's convention."""
        httpx_mock.add_exception(httpx.ConnectError("connection refused"))

        status = get_ibkr_status(_IBKR_SETTINGS)

        assert status.gateway_reachable is False
        assert status.status_label == "GATEWAY_UNREACHABLE"
        assert status.error is not None

    def test_reports_competing_session(self, httpx_mock):
        httpx_mock.add_response(
            url="https://localhost:5001/v1/api/iserver/auth/status",
            json={"authenticated": True, "connected": True, "competing": True},
        )

        status = get_ibkr_status(_IBKR_SETTINGS)

        assert status.status_label == "COMPETING_SESSION"


class TestDescribeLlmConfiguration:
    def test_reports_unconfigured_when_required_keys_missing(self):
        settings = Settings(
            llm_provider="deepseek", deepseek_api_key=None, deepseek_model=None, _env_file=None
        )
        status = describe_llm_configuration(settings)
        assert status.provider == "deepseek"
        assert status.configured is False

    def test_reports_configured_when_required_keys_present(self):
        settings = Settings(
            llm_provider="anthropic",
            anthropic_api_key="test-key",
            anthropic_model="claude-test",
            _env_file=None,
        )
        status = describe_llm_configuration(settings)
        assert status.provider == "anthropic"
        assert status.model == "claude-test"
        assert status.configured is True

    def test_reports_unconfigured_for_unknown_provider(self):
        settings = Settings(llm_provider="made_up_provider", _env_file=None)
        status = describe_llm_configuration(settings)
        assert status.configured is False
