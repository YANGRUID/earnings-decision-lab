"""Structured output for the AI Options Decision Engine (Phase 14.9).

The LLM classifies a direction/volatility view and writes the prose
sections below, grounded only in the real evidence provided (the same
evidence an Earnings Thesis uses, plus the thesis itself once generated)
-- it never invents a number, and the direction/volatility_view fields
are a judgment call about the *qualitative* evidence, not a fabricated
probability. The numeric confidence score is never asked of the LLM at
all -- see analytics/decision/confidence.py, which computes it entirely
from real, already-known signals after this response comes back.
"""

from typing import Literal

from pydantic import BaseModel, Field

DirectionLiteral = Literal["strong_bullish", "bullish", "neutral", "bearish", "strong_bearish"]
VolatilityViewLiteral = Literal["long_vol", "neutral_vol", "short_vol"]


class DecisionView(BaseModel):
    direction: DirectionLiteral = Field(
        description="The directional view best supported by the real evidence provided -- "
        "strong_bullish/bullish/neutral/bearish/strong_bearish. Choose neutral when the "
        "evidence is genuinely mixed or insufficient rather than guessing a direction."
    )
    volatility_view: VolatilityViewLiteral = Field(
        description="Whether the real evidence supports expecting a larger-than-typical move "
        "(long_vol), a smaller-than-typical move (short_vol), or no strong view either way "
        "(neutral_vol). Grounded in the real historical move data and options-implied move "
        "provided, not a guess."
    )
    rationale: str = Field(
        description="Why this direction and volatility view follow from the real evidence "
        "provided -- cite it the same way an Earnings Thesis does."
    )
    bull_case: str = Field(
        description="The strongest real case for a bullish outcome, grounded only in the "
        "evidence provided -- state plainly if the evidence for this case is weak."
    )
    bear_case: str = Field(
        description="The strongest real case for a bearish outcome, grounded only in the "
        "evidence provided -- state plainly if the evidence for this case is weak."
    )
    key_catalysts: str = Field(
        description="Real, cited events or factors from the evidence that could move the "
        "stock -- not generic market catalysts."
    )
    key_risks: str = Field(description="Real, cited risks to this view from the evidence provided.")
    disclaimer: str = Field(
        description="A plain statement that this is not investment advice, no direction or "
        "outcome is guaranteed, and options trading involves real risk of loss. Must never "
        "promise, guarantee, or imply a certain profitable outcome."
    )
