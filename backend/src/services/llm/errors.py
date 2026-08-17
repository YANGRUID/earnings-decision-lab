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
