"""Generic adapter for any other OpenAI-compatible endpoint (a local model
server, a vendor not otherwise named, etc.) — same transport, but
capabilities are declared best-effort since the actual backend is unknown
to this codebase. See docs/llm_providers.md.
"""

from services.llm.openai_compatible import _OpenAICompatibleTransport
from services.llm.types import Capabilities


class OpenAICompatibleProvider(_OpenAICompatibleTransport):
    name = "openai_compatible"
    # Best-effort: this class works with *some* OpenAI-compatible backend
    # the operator configured, whose actual feature support this codebase
    # has no way to verify in advance.
    capabilities = Capabilities(
        supports_structured_output=True,
        supports_tool_calling=True,
        supports_streaming=True,
    )

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        if not base_url:
            raise ValueError("openai_compatible requires OPENAI_COMPATIBLE_BASE_URL")
        super().__init__(api_key=api_key, base_url=base_url, model=model)
