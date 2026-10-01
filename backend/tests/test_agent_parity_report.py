"""Writes the measured parity table the final report quotes.

Kept as a test rather than a script so it runs under the same conftest
(test database, no live sockets) as everything else, and so the numbers in
the report cannot drift from a passing suite.
"""

import json
import os
from pathlib import Path

from _agent_fakes import _StubEmbedder
from test_agent_runtime_parity import CORPUS, _script, corpus_company  # noqa: F401

from evaluation.agent_parity import ParityCase, run_parity_case, summarize

OUTPUT = Path(os.environ.get("PARITY_REPORT_PATH", "")) if os.environ.get(
    "PARITY_REPORT_PATH"
) else None


def test_write_the_parity_table(db_session, corpus_company):  # noqa: F811
    results = []
    for name, intent, tool, arguments in CORPUS:
        factory = _script(intent, tool, arguments)
        results.append(
            run_parity_case(
                db_session,
                ParityCase(
                    name=name,
                    question=name,
                    resolved_tickers=[corpus_company.ticker],
                    company_id=corpus_company.id,
                ),
                legacy_llm=factory(),
                graph_llm=factory(),
                embedder=_StubEmbedder(),
            )
        )
    # One revision case, so the re-verification difference is measured too.
    revision_factory = _script(
        CORPUS[1][1], CORPUS[1][2], CORPUS[1][3], supported=False
    )
    results.append(
        run_parity_case(
            db_session,
            ParityCase(
                name="unsupported draft (revision path)",
                question="summarize filing risk factors",
                resolved_tickers=[corpus_company.ticker],
                company_id=corpus_company.id,
            ),
            legacy_llm=revision_factory(),
            graph_llm=revision_factory(),
            embedder=_StubEmbedder(),
        )
    )

    summary = summarize(results)
    assert summary["failed"] == [], summary["failed"]

    print("\n=== PARITY TABLE ===")
    header = (
        f"{'case':<36} {'schema':<7} {'cites L/G':<10} {'LLM L/G':<8} "
        f"{'tools L/G':<10} {'declared':<24} {'result'}"
    )
    print(header)
    for r in results:
        print(
            f"{r.case:<36} "
            f"{'valid':<7} "
            f"{f'{r.legacy.citation_count}/{r.langgraph.citation_count}':<10} "
            f"{f'{r.legacy.llm_calls}/{r.langgraph.llm_calls}':<8} "
            f"{f'{r.legacy.tool_calls}/{r.langgraph.tool_calls}':<10} "
            f"{','.join(r.declared) or '-':<24} "
            f"{'PASS' if r.passed else 'FAIL'}"
        )
    print(f"\nmax LLM delta: {summary['max_llm_call_delta']}")
    print(f"max tool delta: {summary['max_tool_call_delta']}")
    print(f"gate verdicts: {[r.langgraph.evidence_quality_status for r in results]}")
    print(f"retrieval rounds: {[r.langgraph.retrieval_rounds for r in results]}")
    if OUTPUT:
        OUTPUT.write_text(json.dumps(summary, indent=2, default=str))
        print(f"\nwrote {OUTPUT}")
