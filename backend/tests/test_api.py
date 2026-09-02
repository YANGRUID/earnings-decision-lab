"""API tests. The LLM and embedder dependencies are overridden with stubs
for every test (including research endpoints) — no test in this suite
makes a real network call or spends real money, consistent with the rest
of this project's test suite.
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from models.document_chunk import EMBEDDING_DIM
from rag.embeddings import EmbeddingProvider
from services.llm.base import LLMProvider
from services.llm.types import Capabilities, GenerateResult


class _StubEmbedder(EmbeddingProvider):
    model_name = "stub"
    dimension = EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]


class _StubLLM(LLMProvider):
    name = "stub"
    model = "stub-model"
    capabilities = Capabilities(
        supports_structured_output=True, supports_tool_calling=True, supports_streaming=False
    )

    def generate(self, messages, *, tools=None, temperature=0.0, max_tokens=1024):
        return GenerateResult(content="Stub answer, no tools needed.", tool_calls=[])

    def generate_structured(
        self, messages, schema: type[BaseModel], *, temperature=0.0, max_tokens=1024
    ):
        from schemas.agent import IntentCategory, IntentClassification, VerificationResult
        from schemas.decision import DecisionView
        from schemas.thesis import EarningsThesis

        if schema is IntentClassification:
            return IntentClassification(category=IntentCategory.GENERAL, reasoning="stub")
        if schema is VerificationResult:
            return VerificationResult(supported=True)
        if schema is EarningsThesis:
            return EarningsThesis(
                business_context="Stub business context [1].",
                historical_earnings_pattern="Stub historical pattern.",
                guidance_trend="Stub guidance trend.",
                key_risks="Stub key risks [1].",
                market_setup="Stub market setup.",
                disclaimer="This is not investment advice and no outcome is assured.",
            )
        if schema is DecisionView:
            return DecisionView(
                direction="bullish",
                volatility_view="long_vol",
                rationale="Stub rationale [1].",
                bull_case="Stub bull case.",
                bear_case="Stub bear case.",
                key_catalysts="Stub catalysts.",
                key_risks="Stub key risks.",
                disclaimer="This is not investment advice and no outcome is assured.",
            )
        raise NotImplementedError(schema)

    def stream(self, messages, *, temperature=0.0, max_tokens=1024) -> Iterator[str]:
        raise NotImplementedError


@pytest.fixture(scope="module")
def test_client() -> Iterator[TestClient]:
    from api.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def client(test_client, db_session) -> Iterator[TestClient]:
    from api.deps import get_db, get_embedder, get_llm

    test_client.app.dependency_overrides[get_db] = lambda: db_session
    test_client.app.dependency_overrides[get_llm] = lambda: _StubLLM()
    test_client.app.dependency_overrides[get_embedder] = lambda: _StubEmbedder()
    yield test_client
    test_client.app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def _generous_research_rate_limit(client, monkeypatch):
    """test_client (and its research_rate_limiter) is module-scoped, so real
    request counts accumulate across every test in this file that hits
    /research/query or /research/{symbol}/thesis -- without this, tests
    exercising those endpoints multiple times start failing with a real 429
    depending on test order/count, not because of anything they're actually
    testing. test_research_query_rate_limit_enforced still overrides this
    with its own tight limiter for the duration of that one test."""
    from api.rate_limit import SlidingWindowRateLimiter

    monkeypatch.setattr(
        client.app.state,
        "research_rate_limiter",
        SlidingWindowRateLimiter(max_requests=1000, window_seconds=60.0),
    )


def test_health(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready(client):
    response = client.get("/api/v1/ready")
    assert response.status_code == 200


def test_response_includes_request_id_header(client):
    response = client.get("/api/v1/health")
    assert "X-Request-ID" in response.headers


def test_security_headers_present(client):
    response = client.get("/api/v1/health")
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def _seed_company_with_earnings(db_session):
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from models.company import Company
    from models.earnings_event import EarningsEvent
    from models.earnings_result import EarningsResult

    company = Company(ticker="ZZAPI1", name="ZZ API Test Co", cik="0009980001")
    db_session.add(company)
    db_session.flush()
    event = EarningsEvent(
        company_id=company.id, fiscal_year=2026, fiscal_quarter=2, earnings_date=date(2026, 3, 18)
    )
    db_session.add(event)
    db_session.flush()
    db_session.add(
        EarningsResult(
            earnings_event_id=event.id,
            actual_eps=Decimal("3.08"),
            source_provider="test",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.flush()
    return company, event


def test_list_companies_returns_seeded_companies(client, db_session):
    company, _event = _seed_company_with_earnings(db_session)

    response = client.get("/api/v1/companies")

    assert response.status_code == 200
    tickers = {c["ticker"] for c in response.json()}
    assert company.ticker in tickers


def test_get_company_not_found_returns_404_with_request_id(client):
    response = client.get("/api/v1/companies/NOSUCHTICKER")
    assert response.status_code == 404
    body = response.json()
    assert "not found" in body["error"]
    assert body["request_id"] is not None


def test_list_earnings_filtered_by_ticker(client, db_session):
    company, _event = _seed_company_with_earnings(db_session)

    response = client.get("/api/v1/earnings", params={"ticker": company.ticker, "limit": 3})

    assert response.status_code == 200
    events = response.json()
    assert len(events) == 1


def test_get_earnings_event_detail_includes_nested_company_and_result(client, db_session):
    company, event = _seed_company_with_earnings(db_session)

    response = client.get(f"/api/v1/earnings/{event.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["company"]["ticker"] == company.ticker
    assert body["result"]["actual_eps"] == "3.080000"


def test_get_earnings_event_not_found(client):
    response = client.get("/api/v1/earnings/99999999")
    assert response.status_code == 404


def test_get_earnings_event_never_includes_forward_looking_fields(client, db_session):
    """A past earnings event's detail page must never carry next-period
    analyst estimates or implied move -- those are company-level, always-
    current data that belongs to GET /research/{symbol}/overview instead
    (see api/routers/earnings.py and api/routers/research.py). This is the
    Phase 14 temporal-mixing fix: no such fields exist on the response at
    all anymore, regardless of what else is on record for the company.
    """
    _company, event = _seed_company_with_earnings(db_session)

    response = client.get(f"/api/v1/earnings/{event.id}")

    assert response.status_code == 200
    body = response.json()
    assert "market_expectations" not in body
    assert "implied_move" not in body


def test_get_earnings_event_shows_historical_moves_from_other_events(client, db_session):
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from models.earnings_event import EarningsEvent
    from models.price_reaction import PriceReaction

    company, event = _seed_company_with_earnings(db_session)
    older_event = EarningsEvent(
        company_id=company.id, fiscal_year=2025, fiscal_quarter=4, earnings_date=date(2025, 12, 18)
    )
    db_session.add(older_event)
    db_session.flush()
    db_session.add(
        PriceReaction(
            earnings_event_id=older_event.id,
            next_day_move_pct=Decimal("-0.07"),
            source_provider="test",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    response = client.get(f"/api/v1/earnings/{event.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["historical_moves"]["sample_size"] == 1
    assert body["historical_moves"]["largest_move_pct_signed"] == "-0.070000"


def test_get_earnings_event_historical_moves_null_with_no_other_events(client, db_session):
    company, event = _seed_company_with_earnings(db_session)

    response = client.get(f"/api/v1/earnings/{event.id}")

    assert response.status_code == 200
    assert response.json()["historical_moves"] is None






def test_system_status_reflects_real_counts_and_config(client, db_session):
    _seed_company_with_earnings(db_session)

    response = client.get("/api/v1/system-status")

    assert response.status_code == 200
    body = response.json()
    assert body["counts"]["companies"] >= 1
    assert body["counts"]["earnings_events_with_results"] >= 1
    assert body["embedding_model"]
    assert "provider" in body["llm"]
    assert "configured" in body["llm"]
    assert "available" in body["evaluation"]












def test_research_query_with_stub_llm_no_tools(client):
    response = client.post("/api/v1/research/query", json={"question": "hi there"})
    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Stub answer, no tools needed."
    assert body["trace"]["tool_calls"] == []
    assert body["trace"]["model"] == "stub-model"


def test_research_query_rate_limit_enforced(client, monkeypatch):
    from api.rate_limit import SlidingWindowRateLimiter

    # monkeypatch restores the original limiter after this test, so a
    # tightened limit here can't make a later test in this module flaky.
    monkeypatch.setattr(
        client.app.state,
        "research_rate_limiter",
        SlidingWindowRateLimiter(max_requests=1, window_seconds=60.0),
    )

    first = client.post("/api/v1/research/query", json={"question": "hi"})
    second = client.post("/api/v1/research/query", json={"question": "hi again"})

    assert first.status_code == 200
    assert second.status_code == 429


def test_research_query_persists_a_real_history_row(client, db_session):
    from models.ai_research_query import AIResearchQuery

    response = client.post("/api/v1/research/query", json={"question": "hi there"})
    assert response.status_code == 200

    rows = db_session.query(AIResearchQuery).filter(AIResearchQuery.question == "hi there").all()
    assert len(rows) == 1
    assert rows[0].answer_markdown == "Stub answer, no tools needed."
    assert rows[0].provider == "stub"
    assert rows[0].model == "stub-model"
    assert rows[0].ticker is None
    assert rows[0].company_id is None


def test_research_query_with_ticker_scopes_the_history_row_to_that_company(client, db_session):
    from datetime import UTC, datetime

    from models.company import Company
    from models.research_preparation_job import JobStatus, ResearchPreparationJob

    company = Company(ticker="ZZRQAPI", name="ZZ Research Query Co", cik="0009999920")
    db_session.add(company)
    db_session.flush()
    # A completed research-preparation job is what makes this ticker
    # "ready" under the real readiness check (api/routers/research.py's
    # research_query, Part A4) -- without one, the request would honestly
    # come back status="preparing" instead of running the agent.
    db_session.add(
        ResearchPreparationJob(
            ticker="ZZRQAPI",
            company_id=company.id,
            status=JobStatus.COMPLETED,
            steps=[],
            started_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    response = client.post(
        "/api/v1/research/query", json={"question": "what changed?", "ticker": "zzrqapi"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "completed"

    history = client.get("/api/v1/research/history", params={"ticker": "ZZRQAPI"})
    assert history.status_code == 200
    items = history.json()
    assert len(items) == 1
    assert items[0]["ticker"] == "ZZRQAPI"
    assert items[0]["question"] == "what changed?"


def test_research_history_lists_newest_first(client):
    client.post("/api/v1/research/query", json={"question": "first question"})
    client.post("/api/v1/research/query", json={"question": "second question"})

    response = client.get("/api/v1/research/history", params={"limit": 2})
    assert response.status_code == 200
    items = response.json()
    assert items[0]["question"] == "second question"
    assert items[1]["question"] == "first question"


def test_research_history_item_can_be_fetched_by_id(client):
    client.post("/api/v1/research/query", json={"question": "fetch me by id"})
    listed = client.get("/api/v1/research/history", params={"limit": 1}).json()
    item_id = listed[0]["id"]

    response = client.get(f"/api/v1/research/history/{item_id}")
    assert response.status_code == 200
    assert response.json()["question"] == "fetch me by id"


def test_research_history_item_not_found_returns_404(client):
    response = client.get("/api/v1/research/history/999999999")
    assert response.status_code == 404


def test_research_history_item_can_be_deleted(client, db_session):
    from models.ai_research_query import AIResearchQuery

    client.post("/api/v1/research/query", json={"question": "delete me"})
    listed = client.get("/api/v1/research/history", params={"limit": 1}).json()
    item_id = listed[0]["id"]

    delete_response = client.delete(f"/api/v1/research/history/{item_id}")
    assert delete_response.status_code == 204
    assert db_session.get(AIResearchQuery, item_id) is None

    second_delete = client.delete(f"/api/v1/research/history/{item_id}")
    assert second_delete.status_code == 404


def test_research_query_for_an_unprepared_company_returns_preparing_status(client, db_session):
    """Part A4/A11 -- a real company that has never been through research
    preparation must never be answered from nothing; it's enqueued
    through the same durable queue the scheduler uses and reported
    honestly as still preparing."""
    from models.company import Company
    from models.research_preparation_job import JobStatus, ResearchPreparationJob

    company = Company(ticker="ZZRQPREP", name="ZZ Unprepared Test Co", cik="0009999921")
    db_session.add(company)
    db_session.commit()

    response = client.post(
        "/api/v1/research/query",
        json={"question": "What's the latest?", "ticker": "zzrqprep"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "preparing"
    assert body["answer"] is None
    assert body["preparing"] == [
        {"ticker": "ZZRQPREP", "job_id": body["preparing"][0]["job_id"], "job_status": "pending"}
    ]

    job = db_session.query(ResearchPreparationJob).filter_by(ticker="ZZRQPREP").one()
    assert job.status == JobStatus.PENDING
    assert job.earnings_calendar_event_id is None


def test_research_query_with_unresolvable_explicit_ticker_returns_company_not_found(
    client, httpx_mock
):
    import re

    httpx_mock.add_response(
        url=re.compile(r"https://www\.sec\.gov/files/company_tickers\.json"), json={}
    )

    response = client.post(
        "/api/v1/research/query",
        json={"question": "What's going on?", "ticker": "ZZRQNO"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "company_not_found"
    assert body["answer"] is None
    assert body["unresolved_tickers"] == ["ZZRQNO"]


def test_research_query_ignores_an_unresolvable_ticker_mentioned_only_in_text(client, httpx_mock):
    """A stray all-caps word in the question that isn't a real, resolvable
    ticker must not block an otherwise-general answer -- only an explicit
    ticker is treated as a hard requirement."""
    import re

    httpx_mock.add_response(
        url=re.compile(r"https://www\.sec\.gov/files/company_tickers\.json"), json={}
    )

    response = client.post(
        "/api/v1/research/query", json={"question": "Tell me about ZZRQGH please"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["answer"] == "Stub answer, no tools needed."
    assert body["unresolved_tickers"] == ["ZZRQGH"]


def test_search_documents_unknown_ticker_returns_empty(client):
    response = client.get(
        "/api/v1/research/documents", params={"query": "anything", "ticker": "NOSUCHTICKER"}
    )
    assert response.status_code == 200
    assert response.json()["citations"] == []


# --- Research preparation orchestration endpoints ---------------------------
#
# `_run_preparation_background` is stubbed out in every test below: it's
# what actually calls out to real providers (SEC/Tiingo/Alpha Vantage/IBKR)
# via a *separate* SessionLocal() that bypasses the db_session test
# fixture's rollback boundary entirely -- letting it run for real here
# would both violate this module's no-real-network-calls rule and leave
# real rows behind in the shared dev database. These tests cover the
# synchronous request-handling contract (validation, idempotency, status/
# overview reads), not the pipeline itself (see
# tests/test_services_research_orchestration.py for that).


@pytest.fixture
def _stub_background_prep(monkeypatch):
    calls = []

    def _stub(ticker, force, embedder):
        calls.append((ticker, force))

    monkeypatch.setattr("api.routers.research._run_preparation_background", _stub)
    return calls


def test_prepare_unsupported_ticker_returns_422(client, _stub_background_prep):
    response = client.post("/api/v1/research/ZZINVALID1/prepare")
    assert response.status_code == 422
    assert _stub_background_prep == []


def test_prepare_known_ticker_schedules_background_job(client, db_session, _stub_background_prep):
    from models.company import Company

    db_session.add(Company(ticker="ZZPREP", name="ZZ Prep Co", cik="0009999910"))
    db_session.flush()

    response = client.post("/api/v1/research/zzprep/prepare")

    assert response.status_code == 200
    body = response.json()
    assert body == {"ticker": "ZZPREP", "status": "queued"}
    assert _stub_background_prep == [("ZZPREP", False)]


def test_refresh_schedules_background_job_with_force(client, db_session, _stub_background_prep):
    from models.company import Company

    db_session.add(Company(ticker="ZZRFSH", name="ZZ Refresh Co", cik="0009999911"))
    db_session.flush()

    response = client.post("/api/v1/research/zzrfsh/refresh")

    assert response.status_code == 200
    assert _stub_background_prep == [("ZZRFSH", True)]


def test_prepare_reuses_already_running_job_without_scheduling_duplicate(
    client, db_session, _stub_background_prep
):
    from datetime import UTC, datetime

    from models.company import Company
    from models.research_preparation_job import JobStatus, PreparationStep, StepStatus

    company = Company(ticker="ZZRUN", name="ZZ Running Co", cik="0009999912")
    db_session.add(company)
    db_session.flush()

    now = datetime.now(UTC)
    from models.research_preparation_job import ResearchPreparationJob

    running_job = ResearchPreparationJob(
        ticker="ZZRUN",
        company_id=company.id,
        status=JobStatus.RUNNING,
        steps=[
            {
                "step": s.value,
                "status": StepStatus.PENDING.value,
                "detail": None,
                "updated_at": now.isoformat(),
            }
            for s in PreparationStep
        ],
        started_at=now,
    )
    db_session.add(running_job)
    db_session.flush()

    response = client.post("/api/v1/research/zzrun/prepare")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == running_job.id
    assert body["status"] == "running"
    assert _stub_background_prep == []  # no duplicate background task scheduled


def test_status_returns_404_when_no_job_exists(client):
    response = client.get("/api/v1/research/ZZNOJOB/status")
    assert response.status_code == 404


def test_status_returns_latest_job(client, db_session):
    from datetime import UTC, datetime

    from models.company import Company
    from models.research_preparation_job import (
        JobStatus,
        PreparationStep,
        ResearchPreparationJob,
        StepStatus,
    )

    company = Company(ticker="ZZSTAT", name="ZZ Status Co", cik="0009999913")
    db_session.add(company)
    db_session.flush()

    now = datetime.now(UTC)
    job = ResearchPreparationJob(
        ticker="ZZSTAT",
        company_id=company.id,
        status=JobStatus.COMPLETED,
        steps=[
            {
                "step": s.value,
                "status": StepStatus.DONE.value,
                "detail": "ok",
                "updated_at": now.isoformat(),
            }
            for s in PreparationStep
        ],
        started_at=now,
        completed_at=now,
    )
    db_session.add(job)
    db_session.flush()

    response = client.get("/api/v1/research/zzstat/status")

    assert response.status_code == 200
    body = response.json()
    assert body["ticker"] == "ZZSTAT"
    assert body["status"] == "completed"
    assert len(body["steps"]) == len(PreparationStep)


def test_overview_unknown_company_returns_honest_nulls(client):
    response = client.get("/api/v1/research/ZZNOCOMP/overview")
    assert response.status_code == 200
    body = response.json()
    assert body["company"] is None
    assert body["latest_job"] is None
    assert body["earnings_events_count"] == 0
    assert body["latest_earnings_estimate"] is None


def test_overview_known_company_reflects_real_counts(client, db_session):
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from models.company import Company
    from models.filing import Filing
    from models.price_bar import PriceBar

    company = Company(ticker="ZZOVW", name="ZZ Overview Co", cik="0009999914")
    db_session.add(company)
    db_session.flush()
    db_session.add(
        PriceBar(
            ticker="ZZOVW",
            company_id=company.id,
            trade_date=date(2025, 1, 1),
            source_provider="fake",
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=1000,
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.add(
        Filing(
            company_id=company.id,
            accession_number="0000000000-25-000099",
            filing_type="FORM_10K",
            filing_date=date(2025, 1, 1),
            cik="0009999914",
            source_url="https://example.com/doc.htm",
            title="ZZOVW 10-K",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    response = client.get("/api/v1/research/zzovw/overview")

    assert response.status_code == 200
    body = response.json()
    assert body["company"]["ticker"] == "ZZOVW"
    assert body["price_bars_count"] == 1
    assert body["filings_count"] == 1


# --- Strategy Lab and AI Earnings Thesis endpoints ---------------------------












def test_earnings_date_override_unknown_company_returns_404(client):
    response = client.post(
        "/api/v1/research/ZZNODATE/earnings-date",
        json={"estimated_report_date": "2099-01-01"},
    )
    assert response.status_code == 404


def test_earnings_date_override_rejects_a_past_date(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZPASTD", name="ZZ Past Date Co", cik="0009999919"))
    db_session.flush()

    response = client.post(
        "/api/v1/research/zzpastd/earnings-date",
        # Definitely in the past regardless of when this test runs.
        json={"estimated_report_date": "2020-01-01"},
    )

    assert response.status_code == 422
    assert "in the past" in response.json()["error"]


def test_earnings_date_override_persists_with_manual_provenance(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZMANUAL", name="ZZ Manual Override Co", cik="0009999920"))
    db_session.flush()

    response = client.post(
        "/api/v1/research/zzmanual/earnings-date",
        json={"estimated_report_date": "2099-06-15"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["estimated_report_date"] == "2099-06-15"
    assert body["date_source"] == "manual"
    assert body["eps_estimate_average"] is None
    assert body["source_provider"] == "manual"

    # And it's now what Strategy Lab / Upcoming Earnings would read as "the"
    # upcoming date for this company.
    overview = client.get("/api/v1/research/zzmanual/overview").json()
    assert overview["latest_earnings_estimate"]["date_source"] == "manual"
    assert overview["latest_earnings_estimate"]["estimated_report_date"] == "2099-06-15"


























def test_thesis_unknown_company_returns_404(client):
    response = client.post("/api/v1/research/ZZNOTHS/thesis")
    assert response.status_code == 404


def test_thesis_generates_from_stub_llm(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZTHAPI", name="ZZ Thesis API Co", cik="0009999917"))
    db_session.flush()

    response = client.post("/api/v1/research/zzthapi/thesis")

    assert response.status_code == 200
    body = response.json()
    assert body["business_context"] == "Stub business context [1]."
    assert "not investment advice" in body["disclaimer"]


def test_thesis_generation_persists_a_real_version_row(client, db_session):
    from models.ai_thesis_version import AIThesisVersion
    from models.company import Company

    company = Company(ticker="ZZTHVER", name="ZZ Thesis Version Co", cik="0009999921")
    db_session.add(company)
    db_session.flush()

    response = client.post("/api/v1/research/zzthver/thesis")
    assert response.status_code == 200

    versions = (
        db_session.query(AIThesisVersion).filter(AIThesisVersion.company_id == company.id).all()
    )
    assert len(versions) == 1
    assert versions[0].business_context == "Stub business context [1]."
    assert versions[0].provider == "stub"


def test_thesis_generating_twice_creates_two_versions_never_overwrites(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZTHV2", name="ZZ Thesis Version Co 2", cik="0009999922"))
    db_session.flush()

    first = client.post("/api/v1/research/zzthv2/thesis")
    second = client.post("/api/v1/research/zzthv2/thesis")
    assert first.status_code == 200
    assert second.status_code == 200

    versions = client.get("/api/v1/research/zzthv2/theses").json()
    assert len(versions) == 2
    # Newest first, and both real, distinct rows -- not one row mutated twice.
    assert versions[0]["id"] != versions[1]["id"]


def test_thesis_history_unknown_ticker_returns_404(client):
    response = client.get("/api/v1/research/ZZNOTHVHIST/theses")
    assert response.status_code == 404


def test_thesis_version_can_be_fetched_by_id(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZTHVFETCH", name="ZZ Thesis Fetch Co", cik="0009999923"))
    db_session.flush()

    client.post("/api/v1/research/zzthvfetch/thesis")
    versions = client.get("/api/v1/research/zzthvfetch/theses").json()
    version_id = versions[0]["id"]

    response = client.get(f"/api/v1/research/zzthvfetch/theses/{version_id}")
    assert response.status_code == 200
    assert response.json()["business_context"] == "Stub business context [1]."


def test_thesis_version_for_a_different_company_returns_404(client, db_session):
    from models.company import Company

    company_a = Company(ticker="ZZTHVA", name="ZZ Thesis Co A", cik="0009999924")
    company_b = Company(ticker="ZZTHVB", name="ZZ Thesis Co B", cik="0009999925")
    db_session.add_all([company_a, company_b])
    db_session.flush()

    client.post("/api/v1/research/zzthva/thesis")
    versions_a = client.get("/api/v1/research/zzthva/theses").json()
    version_id = versions_a[0]["id"]

    response = client.get(f"/api/v1/research/zzthvb/theses/{version_id}")
    assert response.status_code == 404


def test_thesis_version_can_be_deleted(client, db_session):
    from models.ai_thesis_version import AIThesisVersion
    from models.company import Company

    db_session.add(Company(ticker="ZZTHVDEL", name="ZZ Thesis Delete Co", cik="0009999926"))
    db_session.flush()

    client.post("/api/v1/research/zzthvdel/thesis")
    versions = client.get("/api/v1/research/zzthvdel/theses").json()
    version_id = versions[0]["id"]

    delete_response = client.delete(f"/api/v1/research/zzthvdel/theses/{version_id}")
    assert delete_response.status_code == 204
    assert db_session.get(AIThesisVersion, version_id) is None


def test_thesis_is_not_stale_immediately_after_generation(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZTHFRESH", name="ZZ Thesis Fresh Co", cik="0009999927"))
    db_session.flush()

    client.post("/api/v1/research/zzthfresh/thesis")
    versions = client.get("/api/v1/research/zzthfresh/theses").json()

    assert versions[0]["is_stale"] is False


def test_thesis_becomes_stale_once_newer_consensus_data_exists(client, db_session):
    from datetime import UTC, date, datetime

    from models.company import Company
    from models.earnings_estimate_snapshot import EarningsEstimateSnapshot

    company = Company(ticker="ZZTHSTALE", name="ZZ Thesis Stale Co", cik="0009999928")
    db_session.add(company)
    db_session.flush()

    client.post("/api/v1/research/zzthstale/thesis")

    # A real, newer consensus snapshot arrives after the thesis was generated.
    now = datetime.now(UTC)
    db_session.add(
        EarningsEstimateSnapshot(
            company_id=company.id,
            fiscal_period_end_date=date(2026, 10, 31),
            horizon="fiscal quarter",
            snapshot_timestamp=now,
            estimated_report_date=date(2026, 11, 15),
            source_provider="alpha_vantage",
            retrieved_at=now,
        )
    )
    db_session.flush()

    versions = client.get("/api/v1/research/zzthstale/theses").json()
    assert versions[0]["is_stale"] is True


































