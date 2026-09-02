class LLMError(Exception):
    """Base class for every error this layer raises — callers can catch this
    one type without needing to know which provider is configured."""


class UnknownProviderError(LLMError):
    pass


class MissingAPIKeyError(LLMError):
    pass


class LLMRequestError(LLMError):
    """The provider's API returned an error response."""


class StructuredOutputError(LLMError):
    """The model's response couldn't be parsed/validated against the
    requested schema."""


class LLMConfigurationError(LLMError):
    """The requested provider/model/reasoning combination is not something
    this codebase can send honestly (e.g. an explicit thinking configuration
    for a provider whose API has no such field). Raised before any request
    is made -- never downgraded to a different model silently."""
