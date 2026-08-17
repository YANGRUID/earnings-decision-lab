import pytest

from core.config import Settings
from services.llm.anthropic import AnthropicProvider
from services.llm.deepseek import DeepSeekProvider
from services.llm.errors import MissingAPIKeyError, UnknownProviderError
from services.llm.factory import get_llm_provider
from services.llm.openai import OpenAIProvider
from services.llm.openai_compatible_provider import OpenAICompatibleProvider


def _settings(**overrides) -> Settings:
    # _env_file=None guarantees the real project .env (which has a live
    # DeepSeek key) is never read here — every field is exactly what the
    # test passes, nothing more.
    return Settings(_env_file=None, **overrides)


def test_deepseek_selected_and_constructed():
    settings = _settings(
        llm_provider="deepseek", deepseek_api_key="key", deepseek_model="deepseek-v4-flash"
    )
    provider = get_llm_provider(settings)
    assert isinstance(provider, DeepSeekProvider)


def test_deepseek_missing_key_raises():
    settings = _settings(llm_provider="deepseek", deepseek_model="deepseek-v4-flash")
    with pytest.raises(MissingAPIKeyError):
        get_llm_provider(settings)


def test_deepseek_missing_model_raises():
    settings = _settings(llm_provider="deepseek", deepseek_api_key="key")
    with pytest.raises(MissingAPIKeyError):
        get_llm_provider(settings)


def test_openai_selected_and_constructed():
    settings = _settings(llm_provider="openai", openai_api_key="key", openai_model="gpt-x")
    provider = get_llm_provider(settings)
    assert isinstance(provider, OpenAIProvider)


def test_openai_missing_key_raises():
    settings = _settings(llm_provider="openai", openai_model="gpt-x")
    with pytest.raises(MissingAPIKeyError):
        get_llm_provider(settings)


def test_anthropic_selected_and_constructed():
    settings = _settings(
        llm_provider="anthropic", anthropic_api_key="key", anthropic_model="claude-x"
    )
    provider = get_llm_provider(settings)
    assert isinstance(provider, AnthropicProvider)


def test_anthropic_missing_key_raises():
    settings = _settings(llm_provider="anthropic", anthropic_model="claude-x")
    with pytest.raises(MissingAPIKeyError):
        get_llm_provider(settings)


def test_openai_compatible_selected_and_constructed():
    settings = _settings(
        llm_provider="openai_compatible",
        openai_compatible_api_key="key",
        openai_compatible_base_url="https://example.com/v1",
        openai_compatible_model="local-model",
    )
    provider = get_llm_provider(settings)
    assert isinstance(provider, OpenAICompatibleProvider)


def test_openai_compatible_missing_base_url_raises():
    settings = _settings(
        llm_provider="openai_compatible",
        openai_compatible_api_key="key",
        openai_compatible_model="local-model",
    )
    with pytest.raises(MissingAPIKeyError):
        get_llm_provider(settings)


def test_unknown_provider_raises():
    settings = _settings(llm_provider="not-a-real-provider")
    with pytest.raises(UnknownProviderError):
        get_llm_provider(settings)


def test_provider_value_is_case_insensitive():
    settings = _settings(
        llm_provider="DeepSeek", deepseek_api_key="key", deepseek_model="deepseek-v4-flash"
    )
    provider = get_llm_provider(settings)
    assert isinstance(provider, DeepSeekProvider)


@pytest.mark.parametrize(
    ("provider_cls", "capability"),
    [
        (DeepSeekProvider, "supports_tool_calling"),
        (OpenAIProvider, "supports_tool_calling"),
        (AnthropicProvider, "supports_tool_calling"),
    ],
)
def test_capabilities_are_declared(provider_cls, capability):
    assert getattr(provider_cls.capabilities, capability) is True
