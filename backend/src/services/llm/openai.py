"""OpenAI adapter — the canonical OpenAI-compatible shape everything else
in this package is normalized against. See docs/llm_providers.md for the
one real difference worth knowing: OpenAI supports a stricter
schema-constrained ``response_format: json_schema`` mode that this project
does not use, in favor of the plain JSON-mode approach shared with DeepSeek
(portability over squeezing out OpenAI-specific strictness).
"""

from services.llm.openai_compatible import _OpenAICompatibleTransport
from services.llm.types import Capabilities

DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider(_OpenAICompatibleTransport):
    name = "openai"
    capabilities = Capabilities(
        supports_structured_output=True,
        supports_tool_calling=True,
        supports_streaming=True,
    )

    def __init__(self, api_key: str, model: str, base_url: str = DEFAULT_BASE_URL) -> None:
        super().__init__(api_key=api_key, base_url=base_url, model=model)
