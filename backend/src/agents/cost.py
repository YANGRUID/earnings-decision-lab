"""Approximate USD cost estimation from token usage.

Prices are point-in-time snapshots (recorded with the date verified) and
will drift — this is a rough operational estimate for the execution trace,
never authoritative billing. An unrecognized model returns ``None`` rather
than guessing, per this project's rule against fabricating numbers.
"""

from decimal import Decimal

from services.llm.types import TokenUsage

# (input $ / 1M tokens, output $ / 1M tokens). DeepSeek verified live against
# api-docs.deepseek.com/quick_start/pricing on 2026-08-17 (standard, non-cache
# rate used — cache-hit pricing is materially cheaper but not assumed here).
_PRICING_PER_MILLION_TOKENS: dict[str, tuple[Decimal, Decimal]] = {
    "deepseek-v4-flash": (Decimal("0.44"), Decimal("1.32")),
    "deepseek-v4-pro": (Decimal("1.32"), Decimal("3.96")),
}


def estimate_cost_usd(model: str, usage: TokenUsage | None) -> Decimal | None:
    if usage is None:
        return None
    rates = _PRICING_PER_MILLION_TOKENS.get(model)
    if rates is None:
        return None
    input_rate, output_rate = rates
    return (usage.input_tokens * input_rate + usage.output_tokens * output_rate) / Decimal(
        "1000000"
    )
