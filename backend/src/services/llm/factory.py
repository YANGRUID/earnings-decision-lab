"""Constructs the configured LLMProvider from Settings, plus an optional
owner override (see services/provider_settings.py) -- this is the only
place in the codebase that knows LLM_PROVIDER exists; everything else
depends on the LLMProvider interface. With no override, behaves exactly as
before: reads LLM_PROVIDER (and that provider's model) from env vars.
``override_model`` only ever applies to whichever provider is actually
selected (override or env default) -- there is one active model at a time,
matching the Settings UI's own "Provider / Model" pairing.
"""

from core.config import Settings
from services.llm.anthropic import AnthropicProvider
from services.llm.base import LLMProvider
from services.llm.deepseek import DeepSeekProvider
from services.llm.errors import MissingAPIKeyError, UnknownProviderError
from services.llm.openai import OpenAIProvider
from services.llm.openai_compatible_provider import OpenAICompatibleProvider

KNOWN_LLM_PROVIDERS = ("deepseek", "openai", "anthropic", "openai_compatible")


def get_llm_provider(
    settings: Settings,
    override_provider: str | None = None,
    override_model: str | None = None,
) -> LLMProvider:
    provider = (override_provider or settings.llm_provider).lower()

    if provider == "deepseek":
        model = override_model or settings.deepseek_model
        if not settings.deepseek_api_key:
            raise MissingAPIKeyError("LLM provider deepseek requires DEEPSEEK_API_KEY")
        if not model:
            raise MissingAPIKeyError("LLM provider deepseek requires DEEPSEEK_MODEL")
        return DeepSeekProvider(
            api_key=settings.deepseek_api_key,
            model=model,
            base_url=settings.deepseek_base_url,
        )

    if provider == "openai":
        model = override_model or settings.openai_model
        if not settings.openai_api_key:
            raise MissingAPIKeyError("LLM provider openai requires OPENAI_API_KEY")
        if not model:
            raise MissingAPIKeyError("LLM provider openai requires OPENAI_MODEL")
        return OpenAIProvider(api_key=settings.openai_api_key, model=model)

    if provider == "anthropic":
        model = override_model or settings.anthropic_model
        if not settings.anthropic_api_key:
            raise MissingAPIKeyError("LLM provider anthropic requires ANTHROPIC_API_KEY")
        if not model:
            raise MissingAPIKeyError("LLM provider anthropic requires ANTHROPIC_MODEL")
        return AnthropicProvider(api_key=settings.anthropic_api_key, model=model)

    if provider == "openai_compatible":
        model = override_model or settings.openai_compatible_model
        if not settings.openai_compatible_api_key:
            raise MissingAPIKeyError(
                "LLM provider openai_compatible requires OPENAI_COMPATIBLE_API_KEY"
            )
        if not settings.openai_compatible_base_url:
            raise MissingAPIKeyError(
                "LLM provider openai_compatible requires OPENAI_COMPATIBLE_BASE_URL"
            )
        if not model:
            raise MissingAPIKeyError(
                "LLM provider openai_compatible requires OPENAI_COMPATIBLE_MODEL"
            )
        return OpenAICompatibleProvider(
            api_key=settings.openai_compatible_api_key,
            model=model,
            base_url=settings.openai_compatible_base_url,
        )

    raise UnknownProviderError(
        f"unknown LLM provider {provider!r} — expected one of {KNOWN_LLM_PROVIDERS}"
    )
