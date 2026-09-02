"""DeepSeek adapter. Verified against api-docs.deepseek.com (2026-09-02):
OpenAI-compatible /chat/completions shape, JSON mode via
response_format={"type":"json_object"}, function/tool calling via the
standard tools/tool_choice fields. Current model IDs are deepseek-v4-flash
and deepseek-v4-pro — deepseek-chat/deepseek-reasoner were deprecated
2026-07-24 and must not be used as a default.

THINKING MODE. Both V4 models support it and the API enables it BY DEFAULT
with reasoning_effort "high" (so "flash = non-thinking" is not true; flash
is simply the smaller/faster model). The request field is::

    "thinking": {"type": "enabled" | "disabled", "reasoning_effort": "low" | "high" | "max"}

In thinking mode the API returns the hidden reasoning as
``choices[].message.reasoning_content`` beside ``content``, reports
``usage.completion_tokens_details.reasoning_tokens``, does NOT accept
``temperature``/``top_p``/``presence_penalty``/``frequency_penalty``, and
counts the reasoning tokens against ``max_tokens`` (found live 2026-08: a
5-token connectivity check came back empty with finish_reason="length").

This adapter therefore makes thinking an EXPLICIT constructor choice and
always sends it -- never relying on the API default:

* ``thinking="disabled"`` (the constructor default, used by research
  preparation, AI Research and the official V3 engine, whose behaviour
  is unchanged): fast, deterministic, temperature honoured.
* ``thinking="enabled"`` with an explicit ``reasoning_effort`` (the V4
  DecisionView, see services/v4_decision_view_config.py): temperature is
  omitted as the API requires, and callers must pass a max_tokens budget
  large enough for the reasoning plus the visible answer.

The reasoning text is never persisted; only its presence/size and the
provider-reported token counts are (services/llm/openai_compatible.py).
"""

from typing import Literal

from services.llm.openai_compatible import _OpenAICompatibleTransport
from services.llm.types import Capabilities

DEFAULT_BASE_URL = "https://api.deepseek.com"

ThinkingMode = Literal["enabled", "disabled"]
ReasoningEffort = Literal["low", "high", "max"]

SUPPORTED_REASONING_EFFORTS: tuple[str, ...] = ("low", "high", "max")


class DeepSeekProvider(_OpenAICompatibleTransport):
    name = "deepseek"
    capabilities = Capabilities(
        supports_structured_output=True,  # via JSON mode, not schema-constrained decoding
        supports_tool_calling=True,
        supports_streaming=True,
    )

    def __init__(
        self,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        *,
        thinking: ThinkingMode = "disabled",
        reasoning_effort: ReasoningEffort | None = None,
    ) -> None:
        if thinking not in ("enabled", "disabled"):
            raise ValueError(f"deepseek thinking must be 'enabled' or 'disabled', got {thinking!r}")
        if reasoning_effort is not None and reasoning_effort not in SUPPORTED_REASONING_EFFORTS:
            raise ValueError(
                f"deepseek reasoning_effort must be one of {SUPPORTED_REASONING_EFFORTS}, "
                f"got {reasoning_effort!r}"
            )
        if thinking == "disabled" and reasoning_effort is not None:
            raise ValueError("deepseek reasoning_effort requires thinking='enabled'")
        super().__init__(api_key=api_key, base_url=base_url, model=model)
        #: Public, like ``model``: callers that persist provenance read these.
        self.thinking: ThinkingMode = thinking
        self.reasoning_effort: ReasoningEffort | None = reasoning_effort

    def _extra_payload_fields(self) -> dict:
        if self.thinking == "enabled":
            thinking: dict = {"type": "enabled"}
            if self.reasoning_effort is not None:
                thinking["reasoning_effort"] = self.reasoning_effort
            return {"thinking": thinking}
        return {"thinking": {"type": "disabled"}}

    def _sampling_fields(self, temperature: float) -> dict:
        # Documented as unsupported in thinking mode -- omitted, not sent
        # and hoped to be ignored.
        return {} if self.thinking == "enabled" else {"temperature": temperature}
