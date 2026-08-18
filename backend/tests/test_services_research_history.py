from datetime import UTC, date, datetime
from decimal import Decimal

from agents.types import AgentResponse, ExecutionTrace, ToolCallRecord
from models.ai_research_query import AIResearchQuery
from models.ai_thesis_version import AIThesisVersion
from models.company import Company
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from rag.context import Citation
from schemas.thesis import EarningsThesis
from services.earnings_thesis import EarningsThesisResult
from services.research_history import (
    ThesisProvenance,
    delete_research_query,
    delete_thesis_version,
    get_research_query,
    get_thesis_version,
    list_research_queries,
    list_thesis_versions,
    persist_research_query,
    persist_thesis_version,
)


def _seed_company(db_session, ticker: str = "ZZRHIST") -> Company:
    # A stable, unique-per-ticker fake CIK -- avoids ix_company_cik
    # collisions when a test seeds more than one company.
    cik = str(abs(hash(ticker)) % 900000000 + 100000000)
    company = Company(ticker=ticker, name="ZZ Research History Co", cik=cik)
    db_session.add(company)
    db_session.flush()
    return company


def _seed_estimate_snapshot(db_session, company: Company) -> EarningsEstimateSnapshot:
    now = datetime.now(UTC)
    snapshot = EarningsEstimateSnapshot(
        company_id=company.id,
        fiscal_period_end_date=date(2026, 10, 31),
        horizon="fiscal quarter",
        snapshot_timestamp=now,
        estimated_report_date=date(2026, 11, 15),
        source_provider="alpha_vantage",
        retrieved_at=now,
    )
    db_session.add(snapshot)
    db_session.flush()
    return snapshot


def _agent_response(question: str = "a real question") -> AgentResponse:
    trace = ExecutionTrace(
        intent_category="filing_lookup",
        planning_method="native_tool_calling",
        tool_calls=[
            ToolCallRecord(
                tool_name="filings_search",
                arguments={"query": "risk factors"},
                success=True,
                duration_ms=12.5,
                summary="found 3 chunks",
            )
        ],
        verification_ran=True,
        verification_supported=True,
        revised=False,
        model="stub-model",
        total_input_tokens=100,
        total_output_tokens=50,
        estimated_cost_usd=Decimal("0.0012"),
        total_duration_ms=250.0,
    )
    return AgentResponse(
        question=question,
        answer="A real, grounded answer [1].",
        trace=trace,
        citations=[
            Citation(
                marker="[1]",
                ticker="ZZRHIST",
                filing_type="10-K",
                filing_date=date(2026, 2, 1),
                section="Item 1A",
                source_url="https://example.com/filing",
            )
        ],
    )


class TestPersistResearchQuery:
    def test_persists_all_real_fields_from_the_agent_response(self, db_session):
        company = _seed_company(db_session)
        response = _agent_response()

        row = persist_research_query(
            db_session, ticker="ZZRHIST", company=company, provider="deepseek", result=response
        )

        assert row.id is not None
        assert row.ticker == "ZZRHIST"
        assert row.company_id == company.id
        assert row.question == "a real question"
        assert row.answer_markdown == "A real, grounded answer [1]."
        assert row.citations[0]["marker"] == "[1]"
        assert row.intent_category == "filing_lookup"
        assert row.tool_calls[0]["tool_name"] == "filings_search"
        assert row.provider == "deepseek"
        assert row.model == "stub-model"
        assert row.total_input_tokens == 100
        assert row.estimated_cost_usd == Decimal("0.001200")

    def test_ticker_and_company_are_both_nullable(self, db_session):
        response = _agent_response()
        row = persist_research_query(
            db_session, ticker=None, company=None, provider="deepseek", result=response
        )
        assert row.ticker is None
        assert row.company_id is None


class TestListResearchQueries:
    def test_returns_newest_first(self, db_session):
        company = _seed_company(db_session)
        first = persist_research_query(
            db_session,
            ticker="ZZRHIST",
            company=company,
            provider="deepseek",
            result=_agent_response("first"),
        )
        second = persist_research_query(
            db_session,
            ticker="ZZRHIST",
            company=company,
            provider="deepseek",
            result=_agent_response("second"),
        )

        rows = list_research_queries(db_session, ticker="ZZRHIST")

        assert [r.id for r in rows] == [second.id, first.id]

    def test_filters_by_ticker(self, db_session):
        company_a = _seed_company(db_session, ticker="ZZRHISTA")
        company_b = _seed_company(db_session, ticker="ZZRHISTB")
        persist_research_query(
            db_session,
            ticker="ZZRHISTA",
            company=company_a,
            provider="deepseek",
            result=_agent_response("about A"),
        )
        persist_research_query(
            db_session,
            ticker="ZZRHISTB",
            company=company_b,
            provider="deepseek",
            result=_agent_response("about B"),
        )

        rows = list_research_queries(db_session, ticker="ZZRHISTA")

        assert len(rows) == 1
        assert rows[0].question == "about A"

    def test_respects_limit_and_offset(self, db_session):
        company = _seed_company(db_session, ticker="ZZRHISTLIM")
        for i in range(5):
            persist_research_query(
                db_session,
                ticker="ZZRHISTLIM",
                company=company,
                provider="deepseek",
                result=_agent_response(f"question {i}"),
            )

        page1 = list_research_queries(db_session, ticker="ZZRHISTLIM", limit=2, offset=0)
        page2 = list_research_queries(db_session, ticker="ZZRHISTLIM", limit=2, offset=2)

        assert [r.question for r in page1] == ["question 4", "question 3"]
        assert [r.question for r in page2] == ["question 2", "question 1"]

    def test_limit_is_capped_at_max(self, db_session):
        company = _seed_company(db_session, ticker="ZZRHISTCAP")
        for i in range(3):
            persist_research_query(
                db_session,
                ticker="ZZRHISTCAP",
                company=company,
                provider="deepseek",
                result=_agent_response(f"q{i}"),
            )
        rows = list_research_queries(db_session, ticker="ZZRHISTCAP", limit=99999)
        assert len(rows) == 3  # never raises, just bounded by real row count here


class TestGetAndDeleteResearchQuery:
    def test_get_returns_none_for_unknown_id(self, db_session):
        assert get_research_query(db_session, 999999999) is None

    def test_delete_removes_the_row_and_reports_success(self, db_session):
        company = _seed_company(db_session)
        row = persist_research_query(
            db_session,
            ticker="ZZRHIST",
            company=company,
            provider="deepseek",
            result=_agent_response(),
        )

        deleted = delete_research_query(db_session, row.id)

        assert deleted is True
        assert db_session.get(AIResearchQuery, row.id) is None

    def test_delete_returns_false_for_unknown_id(self, db_session):
        assert delete_research_query(db_session, 999999999) is False


def _thesis_result() -> EarningsThesisResult:
    thesis = EarningsThesis(
        business_context="Real business context [1].",
        historical_earnings_pattern="Real pattern.",
        guidance_trend="Real guidance trend.",
        key_risks="Real risks [1].",
        market_setup="Real market setup.",
        disclaimer="This is not investment advice and no outcome is assured.",
    )
    return EarningsThesisResult(
        thesis=thesis,
        citations=[
            Citation(
                marker="[1]",
                ticker="ZZTHIST",
                filing_type="10-K",
                filing_date=date(2026, 2, 1),
                section="Item 1A",
                source_url="https://example.com/filing",
            )
        ],
        generated_at=datetime.now(UTC),
        model="stub-model",
        estimate_snapshot_id=42,
        volatility_snapshot_id=None,
    )


class TestPersistThesisVersion:
    def test_persists_all_real_fields_including_provenance(self, db_session):
        company = _seed_company(db_session, ticker="ZZTHIST")
        snapshot = _seed_estimate_snapshot(db_session, company)
        result = _thesis_result()

        row = persist_thesis_version(
            db_session,
            company=company,
            provider="deepseek",
            result=result,
            provenance=ThesisProvenance(
                estimate_snapshot_id=snapshot.id, volatility_snapshot_id=None
            ),
        )

        assert row.id is not None
        assert row.company_id == company.id
        assert row.business_context == "Real business context [1]."
        assert row.citations[0]["marker"] == "[1]"
        assert row.provider == "deepseek"
        assert row.model == "stub-model"
        assert row.earnings_estimate_snapshot_id == snapshot.id
        assert row.volatility_snapshot_id is None

    def test_two_generations_create_two_distinct_rows_never_overwriting(self, db_session):
        company = _seed_company(db_session, ticker="ZZTHIST2")
        snapshot_1 = _seed_estimate_snapshot(db_session, company)
        result = _thesis_result()

        first = persist_thesis_version(
            db_session,
            company=company,
            provider="deepseek",
            result=result,
            provenance=ThesisProvenance(
                estimate_snapshot_id=snapshot_1.id, volatility_snapshot_id=None
            ),
        )
        second = persist_thesis_version(
            db_session,
            company=company,
            provider="deepseek",
            result=result,
            provenance=ThesisProvenance(estimate_snapshot_id=None, volatility_snapshot_id=None),
        )

        assert first.id != second.id
        versions = list_thesis_versions(db_session, company.id)
        assert len(versions) == 2
        assert first.earnings_estimate_snapshot_id == snapshot_1.id
        assert second.earnings_estimate_snapshot_id is None


class TestListThesisVersions:
    def test_returns_newest_first_scoped_to_company(self, db_session):
        company_a = _seed_company(db_session, ticker="ZZTHISTA")
        company_b = _seed_company(db_session, ticker="ZZTHISTB")
        result = _thesis_result()

        persist_thesis_version(
            db_session,
            company=company_a,
            provider="deepseek",
            result=result,
            provenance=ThesisProvenance(None, None),
        )
        newest_a = persist_thesis_version(
            db_session,
            company=company_a,
            provider="deepseek",
            result=result,
            provenance=ThesisProvenance(None, None),
        )
        persist_thesis_version(
            db_session,
            company=company_b,
            provider="deepseek",
            result=result,
            provenance=ThesisProvenance(None, None),
        )

        versions = list_thesis_versions(db_session, company_a.id)

        assert len(versions) == 2
        assert versions[0].id == newest_a.id


class TestGetAndDeleteThesisVersion:
    def test_get_returns_none_for_unknown_id(self, db_session):
        assert get_thesis_version(db_session, 999999999) is None

    def test_delete_removes_the_row(self, db_session):
        company = _seed_company(db_session, ticker="ZZTHISTDEL")
        row = persist_thesis_version(
            db_session,
            company=company,
            provider="deepseek",
            result=_thesis_result(),
            provenance=ThesisProvenance(None, None),
        )

        assert delete_thesis_version(db_session, row.id) is True
        assert db_session.get(AIThesisVersion, row.id) is None

    def test_delete_returns_false_for_unknown_id(self, db_session):
        assert delete_thesis_version(db_session, 999999999) is False
