PROMPT_VERSION = "agent-intent-v1"

SYSTEM_PROMPT = """\
Classify the user's research question into exactly one category:

- earnings_history: past earnings results, EPS/revenue actuals, price reactions.
- filing_research: what a company said/disclosed in its SEC filings (risk factors, MD&A, etc).
- guidance_comparison: how guidance changed between quarters.
- options_analytics: option strategy payoffs, implied move, or options-chain data.
- general: anything else, including greetings or questions unrelated to these companies.

Pick the single best-fitting category even if the question could touch more than one.
"""
