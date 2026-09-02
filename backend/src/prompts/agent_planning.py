PROMPT_VERSION = "agent-planning-v2"

# Post-live correction (2026-08-25) -- Part A: this prompt used to hardcode
# "a system covering NVDA, AMD, MU, and SNDK" (the original Phase 1 seed
# universe, ingestion/bootstrap_phase1.py::TICKERS) into the LLM's own
# system prompt. That was the real, exact root cause of AI Research
# answering "I don't have any data on INTU... the evidence gathered
# contains historical earnings data for NVDA, AMD, MU, and SNDK" even
# though INTU had already been through Research Preparation and every
# tool below (agents/tools/earnings_history.py, filings_search.py) reads
# real, current, ticker-agnostic tables (Company/EarningsEvent/
# EarningsResult/DocumentChunk) with no hardcoded ticker filter of their
# own -- the LLM was simply told not to bother trying. This project has
# no fixed ticker whitelist; a company is in scope the moment a real
# Company row (or a real SEC CIK) exists for it -- see
# services/symbol_resolution.py's own docstring, already written for
# exactly this purpose, just never wired into this prompt or into
# api/routers/research.py::research_query.
#
# ``known_companies``, when the caller (agents/orchestrator.py::run) has
# already deterministically resolved one or more real tickers from the
# request (services/research_query_resolution.py -- never a guess), is
# injected as an authoritative hint so the LLM reliably scopes its own
# tool calls to that company rather than re-deriving it, imperfectly,
# from free text alone. Still just a hint: the LLM may call tools for a
# different company if the question genuinely names one the resolver
# missed (an honest, disclosed heuristic limitation -- see
# extract_ticker_candidates's own docstring).
_BASE_TOOL_CALLING_PROMPT = """\
You are a research assistant for Earnings Decision Lab. There is no fixed list of covered \
companies -- any company with a real SEC filing history can potentially be researched, once \
that company's own research data has been prepared. You have tools for real historical \
earnings data, real SEC filing search (scoped to one company at a time), real guidance-\
comparison data, and deterministic options calculators.

Call whichever tools are needed to answer the user's question with real data, always passing \
the specific ticker(s) the question is actually about. You may call multiple tools, including \
the same tool for more than one company when the question compares companies -- keep each \
company's evidence separate, never blend one company's filings into another's answer. If a \
tool reports no covered company for a ticker, that company's research has not been prepared \
yet -- say so honestly rather than guessing from training data. If the question needs no tool \
(e.g. a greeting), answer directly without calling any tool. Never answer a factual question \
about a real company from your own training data when a tool could supply real, sourced data \
instead.\
"""


def _known_companies_suffix(known_companies: list[str]) -> str:
    tickers = ", ".join(known_companies)
    many = len(known_companies) > 1
    plural = "companies" if many else "company"
    this_these = "these" if many else "this"
    maybe_s = "s" if many else ""
    return (
        f"\n\nThis question has already been resolved to the following real, "
        f"research-ready {plural}: {tickers}. Scope tool calls to {this_these} "
        f"ticker{maybe_s} unless the question clearly also names another real company."
    )


def build_tool_calling_system_prompt(known_companies: list[str] | None = None) -> str:
    if not known_companies:
        return _BASE_TOOL_CALLING_PROMPT
    return _BASE_TOOL_CALLING_PROMPT + _known_companies_suffix(known_companies)


# Backward-compatible constant for any caller that hasn't been updated to
# the builder above -- identical to build_tool_calling_system_prompt()
# with no known companies.
TOOL_CALLING_SYSTEM_PROMPT = build_tool_calling_system_prompt()

# Used only when the configured provider doesn't support native tool calling
# (services.llm capabilities.supports_tool_calling is False) — see
# agents/orchestrator.py's structured-planner fallback path.
_BASE_STRUCTURED_PLANNER_PROMPT = """\
You are a research assistant for Earnings Decision Lab. There is no fixed list of covered \
companies -- any company with a real SEC filing history can potentially be researched, once \
that company's own research data has been prepared. Given the user's question and the list of \
available tools (name, description, and JSON-schema arguments) below, output a plan: which \
tools to call and with what arguments (always passing the specific ticker(s) the question is \
actually about), as a JSON object matching the required schema. Only include tools that are \
actually needed. If no tool is needed, return an empty items list.

Available tools:
{tool_catalog}\
"""


def build_structured_planner_prompt(
    tool_catalog: str, known_companies: list[str] | None = None
) -> str:
    base = _BASE_STRUCTURED_PLANNER_PROMPT.format(tool_catalog=tool_catalog)
    if not known_companies:
        return base
    return base + _known_companies_suffix(known_companies)
