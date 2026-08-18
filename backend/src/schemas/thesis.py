"""Structured output for the AI Earnings Thesis (Phase 14).

Every section is prose the LLM writes, but only from real evidence
gathered deterministically beforehand (see services/earnings_thesis.py) --
the LLM explains and synthesizes, it never invents a number, a filing
excerpt, or a ranking. ``disclaimer`` is validated (never just trusted)
by services/earnings_thesis.py against a real banned-phrase list before
a thesis is ever returned to a caller -- see PROHIBITED_CERTAINTY_PHRASES.
"""

from pydantic import BaseModel, Field


class EarningsThesis(BaseModel):
    business_context: str = Field(
        description="What the company actually does and what's currently driving the business, "
        "grounded only in the real filing excerpts provided -- not general knowledge."
    )
    historical_earnings_pattern: str = Field(
        description="How this company has actually behaved around past earnings reports, "
        "grounded only in the real historical EPS/revenue/price-reaction evidence provided."
    )
    guidance_trend: str = Field(
        description="Whether the company's own most recent guidance is trending up, down, or "
        "flat versus the prior quarter, grounded only in the real guidance-comparison evidence "
        "provided. State plainly if no guidance comparison is available -- never guess a trend."
    )
    key_risks: str = Field(
        description="Real, cited risk factors or uncertainties from the company's own filings -- "
        "not generic market-risk boilerplate."
    )
    market_setup: str = Field(
        description="Synthesizes the real, already-computed market-expectations, implied-move, "
        "and strategy-ranking numbers provided as evidence -- restates and interprets them, "
        "never recomputes or invents new figures."
    )
    disclaimer: str = Field(
        description="A plain statement that this is not investment advice and no outcome is "
        "guaranteed. Must never promise, guarantee, or imply a certain profitable outcome."
    )
