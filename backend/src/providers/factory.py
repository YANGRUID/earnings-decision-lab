"""Constructs the configured OptionsDataProvider from Settings. This is the
only place in the codebase that knows OPTIONS_PROVIDER exists -- everything
else (ingestion scripts, services/options_analytics.py) depends on the
OptionsDataProvider interface, never on a concrete provider. Mirrors
services/llm/factory.py's pattern.
"""

from core.config import Settings
from providers.alpha_vantage_options import AlphaVantageOptionsProvider
from providers.base import OptionsDataProvider
from providers.ibkr_options import IBKROptionsProvider

_KNOWN_PROVIDERS = ("alpha_vantage", "ibkr")


class UnknownOptionsProviderError(Exception):
    pass


class MissingOptionsProviderConfigError(Exception):
    pass


def get_options_provider(settings: Settings) -> OptionsDataProvider:
    provider = settings.options_provider.lower()

    if provider == "alpha_vantage":
        if not settings.alpha_vantage_api_key:
            raise MissingOptionsProviderConfigError(
                "OPTIONS_PROVIDER=alpha_vantage requires ALPHA_VANTAGE_API_KEY"
            )
        return AlphaVantageOptionsProvider(api_key=settings.alpha_vantage_api_key)

    if provider == "ibkr":
        return IBKROptionsProvider(base_url=settings.ibkr_base_url)

    raise UnknownOptionsProviderError(
        f"unknown OPTIONS_PROVIDER {provider!r} -- expected one of {_KNOWN_PROVIDERS}"
    )
