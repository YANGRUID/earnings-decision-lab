"""The ONE authoritative model/reasoning configuration for the V4
DecisionView (2026-09-02).

Why a separate module: the V4 forward-test cohort's model is part of what
the cohort's evidence means, so it is an explicit, versioned, auditable
setting -- never inherited from ``DEEPSEEK_MODEL`` (which the research
jobs and the official V3 control engine use and which must not move
mid-cohort). Everything that needs to know "what model produces V4 views"
-- the view generator, the Operations page, the Settings page, the
provider Test Connection -- asks this module, so no model name is
scattered through the code.

FAIL CLOSED. If V4 is enabled and the configuration is missing or not
something the provider API can express, ``resolve`` raises
``V4DecisionViewConfigError``. Callers surface it (a V4 shadow event, an
Operations warning); nobody falls back to another model or to the API's
default reasoning mode.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from core.config import Settings

#: Stable identity of this configuration policy, persisted with each view.
V4_DECISION_VIEW_CONFIG_VERSION = "v4-decision-view-model-config-v1"


class V4DecisionViewConfigError(ValueError):
    """The V4 DecisionView cannot be generated honestly with the current
    configuration. Deliberately NOT an LLMError: the view generator's
    LLM-failure handling must not swallow it as a transient model error."""


@dataclass(frozen=True)
class V4DecisionViewConfig:
    provider: str
    model: str
    thinking: str  # "enabled" | "disabled"
    reasoning_effort: str | None  # None when thinking is disabled
    max_tokens: int
    config_version: str = V4_DECISION_VIEW_CONFIG_VERSION

    def as_dict(self) -> dict:
        return asdict(self)


def resolve_v4_decision_view_config(settings: Settings) -> V4DecisionViewConfig:
    provider = (settings.llm_provider or "").lower()
    if provider != "deepseek":
        raise V4DecisionViewConfigError(
            "V4 DecisionView requires LLM_PROVIDER=deepseek: its explicit thinking/"
            f"reasoning configuration maps onto DeepSeek's documented request field, and "
            f"LLM_PROVIDER is {settings.llm_provider!r}"
        )
    model = (settings.v4_decision_view_model or "").strip()
    if not model:
        raise V4DecisionViewConfigError(
            "V4_DECISION_VIEW_MODEL is not set. The V4 DecisionView model must be configured "
            "explicitly (e.g. deepseek-v4-pro); there is no fallback to DEEPSEEK_MODEL"
        )
    thinking = settings.v4_decision_view_thinking
    effort = settings.v4_decision_view_reasoning_effort if thinking == "enabled" else None
    max_tokens = settings.v4_decision_view_max_tokens
    if thinking == "enabled" and max_tokens < 4096:
        raise V4DecisionViewConfigError(
            f"V4_DECISION_VIEW_MAX_TOKENS={max_tokens} is too small for thinking mode: hidden "
            "reasoning tokens count against max_tokens and would truncate the JSON answer"
        )
    return V4DecisionViewConfig(
        provider="deepseek",
        model=model,
        thinking=thinking,
        reasoning_effort=effort,
        max_tokens=max_tokens,
    )


def describe_v4_decision_view_config(settings: Settings) -> dict:
    """Read-model form for Operations / Settings: never raises. Carries the
    configuration error text instead so the UI can show it."""
    try:
        cfg = resolve_v4_decision_view_config(settings)
    except V4DecisionViewConfigError as exc:
        return {
            "provider": (settings.llm_provider or "").lower() or None,
            "model": settings.v4_decision_view_model,
            "thinking": settings.v4_decision_view_thinking,
            "reasoning_effort": settings.v4_decision_view_reasoning_effort,
            "max_tokens": settings.v4_decision_view_max_tokens,
            "config_version": V4_DECISION_VIEW_CONFIG_VERSION,
            "config_error": str(exc),
        }
    return {**cfg.as_dict(), "config_error": None}
