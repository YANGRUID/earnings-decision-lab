"""DeepSeek adapter. Verified live against api-docs.deepseek.com (2026-08):
OpenAI-compatible /chat/completions shape, JSON mode via
response_format={"type":"json_object"}, function/tool calling via the
standard tools/tool_choice fields. Current model IDs are deepseek-v4-flash
and deepseek-v4-pro — deepseek-chat/deepseek-reasoner were deprecated
2026-07-24 and must not be used as a default.

V4 models default to thinking mode ON (a change from the old deepseek-chat/
deepseek-reasoner split, where thinking was chosen via model name). Found
live: a bare 5-token connectivity check came back with empty content and
finish_reason="length" because the whole token budget went to hidden
reasoning tokens. This adapter disables thinking by default
(``thinking: {"type": "disabled"}``, per api-docs.deepseek.com/guides/
thinking_mode) so ``generate()``/``generate_structured()`` are fast and
deterministic by default — matching what callers migrating from the old
deepseek-chat behavior expect, and avoiding silently truncated responses
in latency-sensitive paths like structured extraction and agent tool calls.
"""

from services.llm.openai_compatible import _OpenAICompatibleTransport
from services.llm.types import Capabilities

DEFAULT_BASE_URL = "https://api.deepseek.com"


class DeepSeekProvider(_OpenAICompatibleTransport):
    name = "deepseek"
    capabilities = Capabilities(
        supports_structured_output=True,  # via JSON mode, not schema-constrained decoding
        supports_tool_calling=True,
        supports_streaming=True,
    )

    def __init__(self, api_key: str, model: str, base_url: str = DEFAULT_BASE_URL) -> None:
        super().__init__(api_key=api_key, base_url=base_url, model=model)

    def _extra_payload_fields(self) -> dict:
        return {"thinking": {"type": "disabled"}}
