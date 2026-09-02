from datetime import UTC, date, datetime
from decimal import Decimal

from core.config import Settings
from models.company import Company
from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import (
    EarningsCalendarEventStatus,
    EarningsSource,
    EarningsTiming,
    OptionType,
    ProviderHealthStatus,
)
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from services.provider_settings import ProviderSettingsUpdate, update_app_provider_settings
from services.provider_status import (
    PROVIDER_CAPABILITIES,
    get_provider_dashboard,
    record_health_event,
)


def _settings(**overrides) -> Settings:
    defaults = dict(
        tiingo_api_key="tiingo-real-secret-key-9fd2",
        alpha_vantage_api_key="av-real-secret-key-eq5q",
        deepseek_api_key="deepseek-real-secret",
        openai_api_key=None,
        anthropic_api_key=None,
        openai_compatible_api_key=None,
        _env_file=None,
    )
    defaults.update(overrides)
    return Settings(**defaults)


class TestGetProviderDashboard:
    def test_default_price_history_resolution_has_tiingo_primary_and_av_fallback(
        self, clean_provider_state
    ):
        db_session = clean_provider_state
        domains = get_provider_dashboard(db_session, _settings())
        price_history = next(d for d in domains if d.domain == "price_history")
        assert price_history.primary == "tiingo"
        assert price_history.fallback == "alpha_vantage"
        assert price_history.primary_is_override is False
        assert price_history.fallback_is_override is False

    def test_explicit_primary_override_suppresses_the_implicit_fallback_default(self, db_session):
        update_app_provider_settings(
            db_session, ProviderSettingsUpdate(price_history_primary="alpha_vantage")
        )
        domains = get_provider_dashboard(db_session, _settings())
        price_history = next(d for d in domains if d.domain == "price_history")
        assert price_history.primary == "alpha_vantage"
        assert price_history.fallback is None
        assert price_history.primary_is_override is True

    def test_options_primary_defaults_to_settings_options_provider(self, db_session):
        domains = get_provider_dashboard(db_session, _settings(options_provider="ibkr"))
        options = next(d for d in domains if d.domain == "options")
        assert options.primary == "ibkr"
        assert options.primary_is_override is False

    def test_options_primary_override_wins_over_settings_default(self, db_session):
        update_app_provider_settings(db_session, ProviderSettingsUpdate(options_primary="ibkr"))
        domains = get_provider_dashboard(db_session, _settings(options_provider="alpha_vantage"))
        options = next(d for d in domains if d.domain == "options")
        assert options.primary == "ibkr"
        assert options.primary_is_override is True

    def test_llm_primary_defaults_to_settings_llm_provider(self, db_session):
        domains = get_provider_dashboard(db_session, _settings())
        llm = next(d for d in domains if d.domain == "llm")
        assert llm.primary == "deepseek"
        assert llm.fallback is None

    def test_single_provider_domains_never_expose_a_fallback(self, db_session):
        domains = get_provider_dashboard(db_session, _settings())
        for domain_name in ("earnings_estimates", "filings"):
            domain = next(d for d in domains if d.domain == domain_name)
            assert domain.fallback is None
            assert len(domain.providers) == 1

    def test_masked_key_reflects_the_real_configured_key_never_the_full_value(self, db_session):
        domains = get_provider_dashboard(db_session, _settings())
        price_history = next(d for d in domains if d.domain == "price_history")
        tiingo = next(p for p in price_history.providers if p.provider == "tiingo")
        assert tiingo.configured is True
        assert tiingo.masked_key == "•" * 8 + "9fd2"
        assert "real-secret" not in tiingo.masked_key

    def test_unconfigured_provider_reports_configured_false_and_no_masked_key(self, db_session):
        domains = get_provider_dashboard(db_session, _settings(alpha_vantage_api_key=None))
        price_history = next(d for d in domains if d.domain == "price_history")
        av = next(p for p in price_history.providers if p.provider == "alpha_vantage")
        assert av.configured is False
        assert av.masked_key is None

    def test_no_key_concept_providers_are_always_configured(self, db_session):
        domains = get_provider_dashboard(db_session, _settings())
        filings = next(d for d in domains if d.domain == "filings")
        sec_edgar = filings.providers[0]
        assert sec_edgar.provider == "sec_edgar"
        assert sec_edgar.configured is True
        assert sec_edgar.masked_key is None

    def test_capabilities_match_the_hand_reviewed_matrix(self, db_session):
        domains = get_provider_dashboard(db_session, _settings())
        options = next(d for d in domains if d.domain == "options")
        ibkr = next(p for p in options.providers if p.provider == "ibkr")
        assert ibkr.capabilities.options is True
        assert ibkr.capabilities.greeks is True
        assert ibkr.capabilities.prices is False
        assert ibkr.capabilities == PROVIDER_CAPABILITIES["ibkr"]

    def test_alpha_vantage_carries_the_premium_gating_entitlement_note(self, db_session):
        domains = get_provider_dashboard(db_session, _settings())
        price_history = next(d for d in domains if d.domain == "price_history")
        av = next(p for p in price_history.providers if p.provider == "alpha_vantage")
        assert av.entitlement_note is not None
        assert "premium" in av.entitlement_note.lower()

    def test_tiingo_has_no_entitlement_note(self, db_session):
        domains = get_provider_dashboard(db_session, _settings())
        price_history = next(d for d in domains if d.domain == "price_history")
        tiingo = next(p for p in price_history.providers if p.provider == "tiingo")
        assert tiingo.entitlement_note is None

    def test_last_success_at_for_price_history_comes_from_a_real_price_bar_row(
        self, clean_provider_state
    ):
        db_session = clean_provider_state
        company = Company(ticker="ZZPSTAT1", name="ZZ Provider Status Co", cik="0009999801")
        db_session.add(company)
        db_session.flush()
        retrieved_at = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
        db_session.add(
            PriceBar(
                ticker="ZZPSTAT1",
                company_id=company.id,
                trade_date=date(2026, 2, 28),
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10.5"),
                volume=100,
                source_provider="tiingo",
                retrieved_at=retrieved_at,
            )
        )
        db_session.flush()

        domains = get_provider_dashboard(db_session, _settings())
        price_history = next(d for d in domains if d.domain == "price_history")
        tiingo = next(p for p in price_history.providers if p.provider == "tiingo")
        assert tiingo.last_success_at == retrieved_at

    def test_last_success_at_is_none_when_nothing_has_ever_been_ingested(
        self, clean_provider_state
    ):
        db_session = clean_provider_state
        domains = get_provider_dashboard(db_session, _settings())
        options = next(d for d in domains if d.domain == "options")
        ibkr = next(p for p in options.providers if p.provider == "ibkr")
        assert ibkr.last_success_at is None

    def test_llm_last_success_at_comes_only_from_a_connected_health_event(
        self, clean_provider_state
    ):
        db_session = clean_provider_state
        occurred_at = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)
        record_health_event(
            db_session, "deepseek", "llm", ProviderHealthStatus.CONNECTED, None, occurred_at
        )
        domains = get_provider_dashboard(db_session, _settings())
        llm = next(d for d in domains if d.domain == "llm")
        deepseek = next(p for p in llm.providers if p.provider == "deepseek")
        assert deepseek.last_success_at == occurred_at

    def test_llm_last_success_at_ignores_a_failed_health_event(self, clean_provider_state):
        db_session = clean_provider_state
        record_health_event(
            db_session,
            "deepseek",
            "llm",
            ProviderHealthStatus.AUTH_FAILED,
            "bad key",
            datetime(2026, 3, 1, 9, 0, tzinfo=UTC),
        )
        domains = get_provider_dashboard(db_session, _settings())
        llm = next(d for d in domains if d.domain == "llm")
        deepseek = next(p for p in llm.providers if p.provider == "deepseek")
        assert deepseek.last_success_at is None

    def test_last_error_reflects_the_most_recent_non_connected_health_event(self, db_session):
        record_health_event(
            db_session,
            "tiingo",
            "price_history",
            ProviderHealthStatus.RATE_LIMITED,
            "429 too many requests",
            datetime(2026, 3, 1, 9, 0, tzinfo=UTC),
        )
        record_health_event(
            db_session,
            "tiingo",
            "price_history",
            ProviderHealthStatus.AUTH_FAILED,
            "401 unauthorized",
            datetime(2026, 3, 2, 9, 0, tzinfo=UTC),
        )
        domains = get_provider_dashboard(db_session, _settings())
        price_history = next(d for d in domains if d.domain == "price_history")
        tiingo = next(p for p in price_history.providers if p.provider == "tiingo")
        assert tiingo.last_error_status == "auth_failed"
        assert tiingo.last_error_detail == "401 unauthorized"

    def test_options_snapshot_last_success_uses_the_options_snapshot_table(
        self, clean_provider_state
    ):
        db_session = clean_provider_state
        company = Company(ticker="ZZPSTAT2", name="ZZ Provider Status Co 2", cik="0009999802")
        db_session.add(company)
        db_session.flush()
        retrieved_at = datetime(2026, 3, 5, 10, 0, tzinfo=UTC)
        db_session.add(
            OptionsSnapshot(
                company_id=company.id,
                snapshot_timestamp=retrieved_at,
                expiration_date=date(2026, 4, 17),
                strike=Decimal("100"),
                option_type=OptionType.CALL,
                source_provider="ibkr",
                retrieved_at=retrieved_at,
            )
        )
        db_session.flush()

        domains = get_provider_dashboard(db_session, _settings())
        options = next(d for d in domains if d.domain == "options")
        ibkr = next(p for p in options.providers if p.provider == "ibkr")
        assert ibkr.last_success_at == retrieved_at

    def test_an_error_superseded_by_a_later_real_success_is_not_the_current_state(
        self, clean_provider_state
    ):
        # Found live on the Data Providers page (2026-09-02): a day-old
        # client-id collision was still shown as "gateway_offline" while
        # IBKR snapshots had been flowing for the last 42 minutes.
        db_session = clean_provider_state
        company = Company(ticker="ZZPSTAT3", name="ZZ Provider Status Co 3", cik="0009999803")
        db_session.add(company)
        db_session.flush()
        record_health_event(
            db_session,
            "ibkr",
            "options",
            ProviderHealthStatus.GATEWAY_OFFLINE,
            "IB Gateway/TWS client id is already in use (error 326)",
            datetime(2026, 9, 1, 19, 0, tzinfo=UTC),
        )
        retrieved_at = datetime(2026, 9, 2, 23, 0, tzinfo=UTC)
        db_session.add(
            OptionsSnapshot(
                company_id=company.id,
                snapshot_timestamp=retrieved_at,
                expiration_date=date(2026, 10, 16),
                strike=Decimal("100"),
                option_type=OptionType.CALL,
                source_provider="ibkr_tws",
                retrieved_at=retrieved_at,
            )
        )
        db_session.flush()

        domains = get_provider_dashboard(db_session, _settings(options_provider="ibkr"))
        ibkr = next(
            p
            for p in next(d for d in domains if d.domain == "options").providers
            if p.provider == "ibkr"
        )
        assert ibkr.last_success_at == retrieved_at
        assert ibkr.last_error_status is None
        assert ibkr.last_error_at is None
        assert ibkr.last_error_detail is None

        # An error AFTER the last success is the current state again.
        record_health_event(
            db_session,
            "ibkr",
            "options",
            ProviderHealthStatus.GATEWAY_OFFLINE,
            "socket closed",
            datetime(2026, 9, 3, 1, 0, tzinfo=UTC),
        )
        domains = get_provider_dashboard(db_session, _settings(options_provider="ibkr"))
        ibkr = next(
            p
            for p in next(d for d in domains if d.domain == "options").providers
            if p.provider == "ibkr"
        )
        assert ibkr.last_error_status == "gateway_offline"
        assert ibkr.last_error_detail == "socket closed"

    def test_earnings_calendar_primary_is_earningsapi_fallback_is_finnhub(self, db_session):
        domains = get_provider_dashboard(db_session, _settings())
        earnings_calendar = next(d for d in domains if d.domain == "earnings_calendar")
        assert earnings_calendar.primary == "earningsapi"
        assert earnings_calendar.fallback == "finnhub"
        # No AppProviderSettings override mechanism exists for this
        # domain (deliberately not repurposed for calendar sync-state
        # tracking, see EARNINGS_CALENDAR_PROVIDER_ARCHITECTURE_REVIEW.md)
        # -- always reports as the fixed, non-override default.
        assert earnings_calendar.primary_is_override is False
        assert earnings_calendar.fallback_is_override is False
        assert {p.provider for p in earnings_calendar.providers} == {"earningsapi", "finnhub"}

    def test_earnings_calendar_last_success_at_uses_source_specific_rows(self, db_session):
        # earnings_calendar_event has an incoming FK from decision_snapshot
        # (see models/decision_snapshot.py), so it isn't a leaf table --
        # unlike clean_provider_state's own tables, real rows already
        # committed by earlier real syncs against this shared dev Postgres
        # instance can't safely be bulk-deleted here. Using clearly
        # future-dated timestamps instead keeps this test's MAX(updated_at)
        # assertion correct regardless of what real data already exists.
        earningsapi_synced_at = datetime(2030, 1, 1, 6, 0, tzinfo=UTC)
        finnhub_synced_at = datetime(2029, 1, 1, 6, 0, tzinfo=UTC)
        db_session.add_all(
            [
                EarningsCalendarEvent(
                    symbol="ZZPSTATAPI",
                    company_name="Test EarningsAPI Co",
                    earnings_date=date(2026, 9, 1),
                    earnings_time=EarningsTiming.AMC,
                    status=EarningsCalendarEventStatus.UPCOMING,
                    source=EarningsSource.EARNINGSAPI,
                    updated_at=earningsapi_synced_at,
                ),
                EarningsCalendarEvent(
                    symbol="ZZPSTATFH",
                    company_name="Test Finnhub Co",
                    earnings_date=date(2026, 8, 15),
                    earnings_time=EarningsTiming.BMO,
                    status=EarningsCalendarEventStatus.COMPLETED,
                    source=EarningsSource.FINNHUB,
                    updated_at=finnhub_synced_at,
                ),
            ]
        )
        db_session.flush()

        domains = get_provider_dashboard(db_session, _settings())
        earnings_calendar = next(d for d in domains if d.domain == "earnings_calendar")
        earningsapi = next(p for p in earnings_calendar.providers if p.provider == "earningsapi")
        finnhub = next(p for p in earnings_calendar.providers if p.provider == "finnhub")
        assert earningsapi.last_success_at == earningsapi_synced_at
        assert finnhub.last_success_at == finnhub_synced_at

    def test_earnings_calendar_configured_reflects_the_real_env_key(self, db_session):
        domains = get_provider_dashboard(
            db_session, _settings(earningsapi_api_key="ea-real-secret-key-9k2p")
        )
        earnings_calendar = next(d for d in domains if d.domain == "earnings_calendar")
        earningsapi = next(p for p in earnings_calendar.providers if p.provider == "earningsapi")
        finnhub = next(p for p in earnings_calendar.providers if p.provider == "finnhub")
        assert earningsapi.configured is True
        assert earningsapi.masked_key == "•" * 8 + "9k2p"
        assert finnhub.configured is False  # not set in _settings()'s defaults
