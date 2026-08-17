"""Constructs the configured LLMProvider from Settings. This is the only
place in the codebase that knows LLM_PROVIDER exists — everything else
depends on the LLMProvider interface.
"""

from core.config import Settings
from services.llm.anthropic import AnthropicProvider
from services.llm.base import LLMProvider
from services.llm.deepseek import DeepSeekProvider
from services.llm.errors import MissingAPIKeyError, UnknownProviderError
from services.llm.openai import OpenAIProvider
from services.llm.openai_compatible_provider import OpenAICompatibleProvider

_KNOWN_PROVIDERS = ("deepseek", "openai", "anthropic", "openai_compatible")


def get_llm_provider(settings: Settings) -> LLMProvider:
    provider = settings.llm_provider.lower()

    if provider == "deepseek":
        if not settings.deepseek_api_key:
            raise MissingAPIKeyError("LLM_PROVIDER=deepseek requires DEEPSEEK_API_KEY")
        if not settings.deepseek_model:
            raise MissingAPIKeyError("LLM_PROVIDER=deepseek requires DEEPSEEK_MODEL")
        return DeepSeekProvider(
            api_key=settings.deepseek_api_key,
            model=settings.deepseek_model,
            base_url=settings.deepseek_base_url,
        )

    if provider == "openai":
        if not settings.openai_api_key:
            raise MissingAPIKeyError("LLM_PROVIDER=openai requires OPENAI_API_KEY")
        if not settings.openai_model:
            raise MissingAPIKeyError("LLM_PROVIDER=openai requires OPENAI_MODEL")
        return OpenAIProvider(api_key=settings.openai_api_key, model=settings.openai_model)

    if provider == "anthropic":
        if not settings.anthropic_api_key:
            raise MissingAPIKeyError("LLM_PROVIDER=anthropic requires ANTHROPIC_API_KEY")
        if not settings.anthropic_model:
            raise MissingAPIKeyError("LLM_PROVIDER=anthropic requires ANTHROPIC_MODEL")
        return AnthropicProvider(
            api_key=settings.anthropic_api_key, model=settings.anthropic_model
        )

    if provider == "openai_compatible":
        if not settings.openai_compatible_api_key:
            raise MissingAPIKeyError(
                "LLM_PROVIDER=openai_compatible requires OPENAI_COMPATIBLE_API_KEY"
            )
        if not settings.openai_compatible_base_url:
            raise MissingAPIKeyError(
                "LLM_PROVIDER=openai_compatible requires OPENAI_COMPATIBLE_BASE_URL"
            )
        if not settings.openai_compatible_model:
            raise MissingAPIKeyError(
                "LLM_PROVIDER=openai_compatible requires OPENAI_COMPATIBLE_MODEL"
            )
        return OpenAICompatibleProvider(
            api_key=settings.openai_compatible_api_key,
            model=settings.openai_compatible_model,
            base_url=settings.openai_compatible_base_url,
        )

    raise UnknownProviderError(
        f"unknown LLM_PROVIDER {provider!r} — expected one of {_KNOWN_PROVIDERS}"
    )
