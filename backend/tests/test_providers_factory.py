import pytest

from core.config import Settings
from providers.alpha_vantage_options import AlphaVantageOptionsProvider
from providers.factory import (
    MissingOptionsProviderConfigError,
    UnknownOptionsProviderError,
    get_options_provider,
)
from providers.ibkr_options import IBKROptionsProvider


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
