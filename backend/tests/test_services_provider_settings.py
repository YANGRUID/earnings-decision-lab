import pytest

from models.app_provider_settings import AppProviderSettings
from services.provider_settings import (
    ProviderSettingsUpdate,
    UnknownProviderSelectionError,
    get_app_provider_settings,
    update_app_provider_settings,
)


class TestGetAppProviderSettings:
    def test_creates_all_null_singleton_row_on_a_fresh_database(self, db_session):
        row = get_app_provider_settings(db_session)
        assert row.id == 1
        assert row.price_history_primary is None
        assert row.price_history_fallback is None
        assert row.options_primary is None
        assert row.options_fallback is None
        assert row.llm_provider is None
        assert row.llm_model is None

    def test_is_idempotent_and_returns_the_same_row(self, db_session):
        first = get_app_provider_settings(db_session)
        second = get_app_provider_settings(db_session)
        assert first.id == second.id
        assert db_session.query(AppProviderSettings).count() == 1


class TestUpdateAppProviderSettings:
    def test_applies_a_valid_price_history_primary(self, db_session):
        row = update_app_provider_settings(
            db_session, ProviderSettingsUpdate(price_history_primary="alpha_vantage")
        )
        assert row.price_history_primary == "alpha_vantage"

    def test_rejects_an_unknown_price_history_provider(self, db_session):
        with pytest.raises(UnknownProviderSelectionError):
            update_app_provider_settings(
                db_session, ProviderSettingsUpdate(price_history_primary="made_up_provider")
            )
        # Rejected values are never persisted.
        row = get_app_provider_settings(db_session)
        assert row.price_history_primary is None

    def test_rejects_an_unknown_options_provider(self, db_session):
        with pytest.raises(UnknownProviderSelectionError):
            update_app_provider_settings(
                db_session, ProviderSettingsUpdate(options_primary="fake_options_co")
            )

    def test_rejects_an_unknown_llm_provider(self, db_session):
        with pytest.raises(UnknownProviderSelectionError):
            update_app_provider_settings(
                db_session, ProviderSettingsUpdate(llm_provider="fake_llm_co")
            )

    def test_only_touches_fields_the_caller_actually_set(self, db_session):
        update_app_provider_settings(
            db_session, ProviderSettingsUpdate(price_history_primary="tiingo")
        )
        row = update_app_provider_settings(
            db_session, ProviderSettingsUpdate(options_primary="ibkr")
        )
        assert row.price_history_primary == "tiingo"
        assert row.options_primary == "ibkr"

    def test_clear_price_history_fallback_nulls_a_previously_set_value(self, db_session):
        update_app_provider_settings(
            db_session, ProviderSettingsUpdate(price_history_fallback="alpha_vantage")
        )
        row = update_app_provider_settings(
            db_session, ProviderSettingsUpdate(clear_price_history_fallback=True)
        )
        assert row.price_history_fallback is None

    def test_clear_options_fallback_nulls_a_previously_set_value(self, db_session):
        update_app_provider_settings(
            db_session, ProviderSettingsUpdate(options_fallback="alpha_vantage")
        )
        row = update_app_provider_settings(
            db_session, ProviderSettingsUpdate(clear_options_fallback=True)
        )
        assert row.options_fallback is None

    def test_llm_model_override_is_stored_verbatim(self, db_session):
        row = update_app_provider_settings(
            db_session, ProviderSettingsUpdate(llm_model="claude-test-model")
        )
        assert row.llm_model == "claude-test-model"
