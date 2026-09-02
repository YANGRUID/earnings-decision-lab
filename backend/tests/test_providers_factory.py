import pytest

from core.config import Settings
from providers.alpha_vantage_options import AlphaVantageOptionsProvider
from providers.factory import (
    MissingOptionsProviderConfigError,
    UnknownOptionsProviderError,
    get_options_provider,
)
from providers.ibkr_options import IBKROptionsProvider
from providers.ibkr_tws_options import IBKRTWSProvider


def test_returns_alpha_vantage_provider_when_configured():
    settings = Settings(
        options_provider="alpha_vantage", alpha_vantage_api_key="test-key", _env_file=None
    )
    # get_options_provider wraps every real adapter in a usage-tracking
    # proxy (see services/usage_instrumentation.py) -- unwrap to check the
    # real underlying adapter type.
    provider = get_options_provider(settings)
    assert isinstance(provider._inner, AlphaVantageOptionsProvider)  # noqa: SLF001


def test_raises_when_alpha_vantage_configured_without_api_key():
    settings = Settings(
        options_provider="alpha_vantage", alpha_vantage_api_key=None, _env_file=None
    )
    with pytest.raises(MissingOptionsProviderConfigError):
        get_options_provider(settings)


def test_returns_ibkr_provider_when_configured():
    settings = Settings(
        options_provider="ibkr", ibkr_base_url="https://localhost:5001/v1/api", _env_file=None
    )
    provider = get_options_provider(settings)
    assert isinstance(provider._inner, IBKROptionsProvider)  # noqa: SLF001


def test_raises_for_unknown_provider():
    settings = Settings(options_provider="made_up_provider", _env_file=None)
    with pytest.raises(UnknownOptionsProviderError):
        get_options_provider(settings)


def test_defaults_to_alpha_vantage():
    settings = Settings(alpha_vantage_api_key="test-key", _env_file=None)
    assert settings.options_provider == "alpha_vantage"


class TestIbkrTransportSelection:
    """IBKR TWS Migration Phase 1 -- ibkr_provider selects which real IBKR
    transport a constructed "ibkr" options provider speaks, defaulting to
    "web" (the existing Client Portal Gateway adapter, unchanged) unless
    explicitly set to "tws". No existing deployment's behavior changes
    unless this new setting is explicitly configured."""

    def test_defaults_to_web_transport_when_unset(self):
        settings = Settings(options_provider="ibkr", _env_file=None)
        assert settings.ibkr_provider == "web"
        provider = get_options_provider(settings)
        assert isinstance(provider._inner, IBKROptionsProvider)  # noqa: SLF001

    def test_selects_tws_transport_when_explicitly_configured(self):
        settings = Settings(
            options_provider="ibkr",
            ibkr_provider="tws",
            ibkr_tws_host="host.docker.internal",
            ibkr_tws_port=4002,
            ibkr_tws_client_id=101,
            _env_file=None,
        )
        provider = get_options_provider(settings)
        assert isinstance(provider._inner, IBKRTWSProvider)  # noqa: SLF001

    def test_unrecognized_ibkr_provider_value_falls_back_to_web(self):
        """A typo in this new setting must degrade to this project's
        existing, already-real behavior, never break options collection
        outright."""
        settings = Settings(
            options_provider="ibkr", ibkr_provider="not_a_real_transport", _env_file=None
        )
        provider = get_options_provider(settings)
        assert isinstance(provider._inner, IBKROptionsProvider)  # noqa: SLF001

    def test_build_options_provider_chain_also_respects_tws_selection(self):
        from providers.factory import build_options_provider_chain

        settings = Settings(
            options_provider="ibkr", ibkr_provider="tws", ibkr_tws_client_id=101, _env_file=None
        )
        provider = build_options_provider_chain(settings)
        assert isinstance(provider._inner, IBKRTWSProvider)  # noqa: SLF001
