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


def test_replay_summary_empty_options_data_still_lists_companies(client, db_session):
    company, _event = _seed_company_with_earnings(db_session)

    response = client.get("/api/v1/replay")

    assert response.status_code == 200
    body = response.json()
    assert body["options_data_ingested"] is False
    tickers = {c["company"]["ticker"] for c in body["companies"]}
    assert company.ticker in tickers
    entry = next(c for c in body["companies"] if c["company"]["ticker"] == company.ticker)
    assert entry["implied_vs_realized"] == []


def test_replay_summary_includes_real_historical_moves_and_implied_vs_realized(client, db_session):
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from models.enums import OptionType
    from models.options_snapshot import OptionsSnapshot
    from models.price_reaction import PriceReaction
    from models.volatility_snapshot import VolatilitySnapshot

    company, event = _seed_company_with_earnings(db_session)
    db_session.add(
        PriceReaction(
            earnings_event_id=event.id,
            next_day_move_pct=Decimal("-0.06"),
            source_provider="test",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.add(
        VolatilitySnapshot(
            company_id=company.id,
            snapshot_timestamp=datetime(2026, 3, 10, tzinfo=UTC),
            method="atm_straddle",
            target_earnings_date=event.earnings_date,
            implied_move_pct=Decimal("0.073"),
            computed_at=datetime.now(UTC),
        )
    )
    db_session.add(
        OptionsSnapshot(
            company_id=company.id,
            snapshot_timestamp=datetime(2026, 3, 10, tzinfo=UTC),
            expiration_date=date(2026, 3, 20),
            strike=Decimal("100"),
            option_type=OptionType.CALL,
            source_provider="test",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.flush()

    response = client.get("/api/v1/replay")

    assert response.status_code == 200
    body = response.json()
    assert body["options_data_ingested"] is True
    entry = next(c for c in body["companies"] if c["company"]["ticker"] == company.ticker)
    assert entry["historical_moves"]["sample_size"] == 1
    assert len(entry["implied_vs_realized"]) == 1
    assert entry["implied_vs_realized"][0]["realized_next_day_move_pct"] == "-0.060000"


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


def test_portfolio_positions_empty_when_nothing_collected(client):
    response = client.get("/api/v1/portfolio/positions")

    assert response.status_code == 200
    body = response.json()
    assert body["positions"] == []
    assert body["snapshot_timestamp"] is None


def test_portfolio_positions_reflects_latest_real_snapshot(client, db_session):
    from datetime import UTC, datetime
    from decimal import Decimal

    from models.portfolio_position_snapshot import PortfolioPositionSnapshot

    ts = datetime.now(UTC)
    db_session.add(
        PortfolioPositionSnapshot(
            account_id_masked="U99****99",
            snapshot_timestamp=ts,
            conid=672387468,
            contract_description="MNQ MAR2025",
            asset_class="FUT",
            quantity=Decimal("2"),
            currency="USD",
            market_value=Decimal("87081.72"),
            unrealized_pnl=Decimal("9.48"),
            source_provider="ibkr",
            retrieved_at=ts,
        )
    )
    db_session.flush()

    response = client.get("/api/v1/portfolio/positions")

    assert response.status_code == 200
    body = response.json()
    assert len(body["positions"]) == 1
    assert body["positions"][0]["account_id_masked"] == "U99****99"
    assert body["positions"][0]["market_value"] == "87081.720000"
    # never the real, unmasked account number anywhere in the response
    assert "U99999999" not in response.text


def test_options_payoff_bull_call_spread(client):
    response = client.post(
        "/api/v1/options/strategies/payoff",
        json={
            "strategy_label": "bull call spread",
            "legs": [
                {"option_type": "call", "action": "buy", "strike": "100", "premium": "6"},
                {"option_type": "call", "action": "sell", "strike": "110", "premium": "2"},
            ],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["max_profit"] == "6"
    assert body["max_loss"] == "4"


def test_options_payoff_invalid_request_returns_422(client):
    response = client.post("/api/v1/options/strategies/payoff", json={"strategy_label": "x"})
    assert response.status_code == 422


def test_implied_move(client):
    response = client.post(
        "/api/v1/options/implied-move",
        json={
            "underlying_price": "114.50",
            "strike": "115",
            "call_price": "4.30",
            "put_price": "4.10",
        },
    )
    assert response.status_code == 200
    assert response.json()["implied_move_absolute"] == "8.40"


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
    from models.company import Company

    company = Company(ticker="ZZRQAPI", name="ZZ Research Query Co", cik="0009999920")
    db_session.add(company)
    db_session.flush()

    response = client.post(
        "/api/v1/research/query", json={"question": "what changed?", "ticker": "zzrqapi"}
    )
    assert response.status_code == 200

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


def test_strategy_lab_unknown_company_returns_404(client):
    response = client.get("/api/v1/research/ZZNOSTR/strategies")
    assert response.status_code == 404


def test_strategy_lab_rejects_percent_risk_cap_above_100(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZSLABRISK", name="ZZ Strategy Lab Risk Co", cik="0009999916"))
    db_session.flush()

    response = client.get(
        "/api/v1/research/zzslabrisk/strategies",
        params={"budget": "10000", "risk_cap": "5000", "risk_cap_is_percent": "true"},
    )

    assert response.status_code == 422
    assert "risk_cap_is_percent" in response.json()["error"]


def test_strategy_lab_no_earnings_estimate_explains_missing_anchor_date(client, db_session):
    """Regression test for a real bug found live-debugging AMD (2026-08-18):
    the endpoint returned an empty ``strategies``/``chain`` shell with no
    field explaining why, identical whether the cause was "no known
    upcoming earnings date" or "date known but no chain collected yet" --
    see the endpoint's old docstring, which admitted as much. This is the
    "no upcoming earnings date on record at all" branch (AMD's real state:
    Alpha Vantage's EARNINGS_CALENDAR had zero rows for it at any horizon).
    """
    from models.company import Company

    db_session.add(Company(ticker="ZZSLAB", name="ZZ Strategy Lab Co", cik="0009999915"))
    db_session.flush()

    response = client.get("/api/v1/research/zzslab/strategies")

    assert response.status_code == 200
    body = response.json()
    assert body["strategies"] == []
    assert body["expiration"] is None
    assert body["reason"] is not None
    assert "next earnings date isn't known yet" in body["reason"]


def test_strategy_lab_real_chain_but_no_priceable_quotes_explains_why(client, db_session):
    """Regression test for a real bug found live-verifying the AMD fix
    (2026-08-18, pre-market): a real 22-contract chain had been collected
    (every contract FROZEN with real IV/Greeks) but every bid/ask/last was
    null -- compute_and_persist_volatility_snapshot correctly returned None
    (NoQuoteAvailable, nothing to price a straddle from), but the endpoint's
    reason text falsely claimed "no options-chain snapshot has been
    collected yet either" even though `chain` in the same response was
    non-empty. The reason must reflect what `chain` actually shows.
    """
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from models.company import Company
    from models.enums import OptionsSnapshotAnchor, OptionType
    from models.options_snapshot import OptionsSnapshot

    company = Company(ticker="ZZSLAB5", name="ZZ Strategy Lab Co 5", cik="0009999921")
    db_session.add(company)
    db_session.flush()

    now = datetime.now(UTC)
    for option_type in (OptionType.CALL, OptionType.PUT):
        db_session.add(
            OptionsSnapshot(
                company_id=company.id,
                snapshot_timestamp=now,
                expiration_date=date(2026, 8, 19),
                strike=Decimal("490"),
                option_type=option_type,
                bid=None,
                ask=None,
                last_price=None,
                implied_volatility=Decimal("0.56"),
                market_data_quality="frozen",
                source_provider="ibkr",
                retrieved_at=now,
                anchor=OptionsSnapshotAnchor.GENERAL_CURRENT,
            )
        )
    db_session.flush()

    response = client.get("/api/v1/research/zzslab5/strategies")

    assert response.status_code == 200
    body = response.json()
    assert body["strategies"] == []
    assert len(body["chain"]) == 2
    assert body["reason"] is not None
    assert "real options-chain snapshot exists" in body["reason"]
    assert "no options-chain snapshot has been collected" not in body["reason"]


def test_strategy_lab_known_estimate_but_no_chain_explains_missing_snapshot(client, db_session):
    """The other empty-state branch: an upcoming earnings date *is* known
    (a real EarningsEstimateSnapshot exists) but no options-chain snapshot
    has been collected for it -- must not be confused with "no date known
    at all" (see the sibling test above)."""
    from datetime import UTC, date, datetime

    from models.company import Company
    from models.earnings_estimate_snapshot import EarningsEstimateSnapshot

    company = Company(ticker="ZZSLAB3", name="ZZ Strategy Lab Co 3", cik="0009999917")
    db_session.add(company)
    db_session.flush()

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

    response = client.get("/api/v1/research/zzslab3/strategies")

    assert response.status_code == 200
    body = response.json()
    assert body["strategies"] == []
    assert body["reason"] is not None
    assert "2026-11-15" in body["reason"]
    assert "no real options-chain snapshot has been collected" in body["reason"]


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


def test_strategy_lab_returns_real_ranked_strategies_from_a_real_chain(client, db_session):
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from models.company import Company
    from models.enums import OptionType
    from models.options_snapshot import OptionsSnapshot
    from models.price_bar import PriceBar
    from models.volatility_snapshot import VolatilitySnapshot

    company = Company(ticker="ZZSLAB2", name="ZZ Strategy Lab Co 2", cik="0009999916")
    db_session.add(company)
    db_session.flush()

    now = datetime.now(UTC)
    db_session.add(
        PriceBar(
            ticker="ZZSLAB2",
            company_id=company.id,
            trade_date=date(2026, 8, 1),
            source_provider="test",
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1000,
            retrieved_at=now,
        )
    )
    snapshot_ts = now
    for strike in (Decimal("95"), Decimal("100"), Decimal("105")):
        for option_type in (OptionType.CALL, OptionType.PUT):
            db_session.add(
                OptionsSnapshot(
                    company_id=company.id,
                    snapshot_timestamp=snapshot_ts,
                    expiration_date=date(2026, 9, 18),
                    strike=strike,
                    option_type=option_type,
                    bid=Decimal("1.90"),
                    ask=Decimal("2.10"),
                    source_provider="test",
                    retrieved_at=snapshot_ts,
                )
            )
    db_session.add(
        VolatilitySnapshot(
            company_id=company.id,
            snapshot_timestamp=snapshot_ts,
            method="atm_straddle",
            target_earnings_date=date(2026, 9, 10),
            near_term_expiration=date(2026, 9, 18),
            implied_move_pct=Decimal("0.05"),
            implied_move_absolute=Decimal("5.00"),
            computed_at=now,
        )
    )
    db_session.flush()

    response = client.get("/api/v1/research/zzslab2/strategies")

    assert response.status_code == 200
    body = response.json()
    assert body["strategies"] != []
    assert body["chain"] != []
    assert body["implied_move_pct"] == "0.050000"
    assert body["anchor"] == "earnings_anchored"
    assert body["reason"] is None
    top = body["strategies"][0]
    assert top["rank"] == 1
    assert "ZZSLAB2" in top["explanation"]


def test_strategy_lab_general_current_still_returns_real_strategies(client, db_session):
    """Regression test for the real bug found live-debugging AMD
    (2026-08-18): Strategy Lab must not be an all-or-nothing gate on a
    known earnings date. When a real options-chain snapshot exists but
    isn't earnings-anchored (no reliable date on record), this must still
    return real strategies/chain -- just labeled anchor="general_current"
    with a disclaimer, never an empty result. See
    api/routers/research.py::get_strategy_lab.
    """
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from models.company import Company
    from models.enums import OptionsSnapshotAnchor, OptionType
    from models.options_snapshot import OptionsSnapshot
    from models.price_bar import PriceBar
    from models.volatility_snapshot import VolatilitySnapshot

    company = Company(ticker="ZZSLAB4", name="ZZ Strategy Lab Co 4", cik="0009999918")
    db_session.add(company)
    db_session.flush()

    now = datetime.now(UTC)
    db_session.add(
        PriceBar(
            ticker="ZZSLAB4",
            company_id=company.id,
            trade_date=date(2026, 8, 1),
            source_provider="test",
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1000,
            retrieved_at=now,
        )
    )
    snapshot_ts = now
    for strike in (Decimal("95"), Decimal("100"), Decimal("105")):
        for option_type in (OptionType.CALL, OptionType.PUT):
            db_session.add(
                OptionsSnapshot(
                    company_id=company.id,
                    snapshot_timestamp=snapshot_ts,
                    expiration_date=date(2026, 9, 18),
                    strike=strike,
                    option_type=option_type,
                    bid=Decimal("1.90"),
                    ask=Decimal("2.10"),
                    source_provider="test",
                    retrieved_at=snapshot_ts,
                    anchor=OptionsSnapshotAnchor.GENERAL_CURRENT,
                )
            )
    db_session.add(
        VolatilitySnapshot(
            company_id=company.id,
            snapshot_timestamp=snapshot_ts,
            method="atm_straddle",
            target_earnings_date=None,
            anchor=OptionsSnapshotAnchor.GENERAL_CURRENT,
            near_term_expiration=date(2026, 9, 18),
            implied_move_pct=Decimal("0.05"),
            implied_move_absolute=Decimal("5.00"),
            computed_at=now,
        )
    )
    db_session.flush()

    response = client.get("/api/v1/research/zzslab4/strategies")

    assert response.status_code == 200
    body = response.json()
    assert body["strategies"] != []
    assert body["chain"] != []
    assert body["anchor"] == "general_current"
    assert body["reason"] is not None
    assert "not currently confirmed" in body["reason"]
    assert "not earnings-anchored" in body["reason"]


def test_strategy_lab_exposes_real_state_bar_provenance_for_a_stale_snapshot(client, db_session):
    """The state bar (market_session/data_state/snapshot_source/snapshot_age
    /earnings_anchor_status) must be real and consistent, and a snapshot
    from a prior calendar day must be labeled previous_session -- never
    presented as current. A fixed, clearly-past snapshot_timestamp keeps
    this deterministic regardless of when the suite actually runs."""
    from datetime import UTC, date, datetime
    from decimal import Decimal

    from models.company import Company
    from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
    from models.enums import OptionType, UpcomingEarningsDateSource
    from models.options_snapshot import OptionsSnapshot
    from models.price_bar import PriceBar
    from models.volatility_snapshot import VolatilitySnapshot

    company = Company(ticker="ZZSLAB5", name="ZZ Strategy Lab Co 5", cik="0009999919")
    db_session.add(company)
    db_session.flush()

    now = datetime.now(UTC)
    stale_snapshot_ts = datetime(2020, 1, 15, 15, 0, tzinfo=UTC)
    db_session.add(
        PriceBar(
            ticker="ZZSLAB5",
            company_id=company.id,
            trade_date=date(2020, 1, 15),
            source_provider="test",
            open=Decimal("100"),
            high=Decimal("101"),
            low=Decimal("99"),
            close=Decimal("100"),
            volume=1000,
            retrieved_at=stale_snapshot_ts,
        )
    )
    db_session.add(
        EarningsEstimateSnapshot(
            company_id=company.id,
            fiscal_period_end_date=date(2020, 4, 30),
            horizon="fiscal quarter",
            snapshot_timestamp=stale_snapshot_ts,
            estimated_report_date=date(2020, 1, 20),
            date_source=UpcomingEarningsDateSource.MANUAL,
            source_provider="manual",
            retrieved_at=stale_snapshot_ts,
        )
    )
    for strike in (Decimal("95"), Decimal("100"), Decimal("105")):
        for option_type in (OptionType.CALL, OptionType.PUT):
            db_session.add(
                OptionsSnapshot(
                    company_id=company.id,
                    snapshot_timestamp=stale_snapshot_ts,
                    expiration_date=date(2020, 2, 21),
                    strike=strike,
                    option_type=option_type,
                    bid=Decimal("1.90"),
                    ask=Decimal("2.10"),
                    market_data_quality="live",
                    source_provider="ibkr",
                    retrieved_at=stale_snapshot_ts,
                )
            )
    db_session.add(
        VolatilitySnapshot(
            company_id=company.id,
            snapshot_timestamp=stale_snapshot_ts,
            method="atm_straddle",
            target_earnings_date=date(2020, 1, 20),
            near_term_expiration=date(2020, 2, 21),
            implied_move_pct=Decimal("0.05"),
            implied_move_absolute=Decimal("5.00"),
            computed_at=now,
        )
    )
    db_session.flush()

    response = client.get("/api/v1/research/zzslab5/strategies")

    assert response.status_code == 200
    body = response.json()
    assert body["data_state"] == "previous_session"
    assert body["snapshot_source"] == "ibkr"
    assert body["snapshot_timestamp"] is not None
    assert body["snapshot_age_minutes"] is not None and body["snapshot_age_minutes"] > 0
    assert body["snapshot_age_label"] not in (None, "")
    assert body["earnings_anchor_status"] == "manual"
    assert body["market_session"] in (
        "pre_market",
        "regular",
        "after_hours",
        "closed",
    )
    # Real strategies are still generated from the stale data -- the UI, not
    # the API, is responsible for the "stale, use with care" presentation.
    assert body["strategies"] != []


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


def test_decision_unknown_company_returns_404(client):
    response = client.post("/api/v1/research/ZZNODEC/decision")
    assert response.status_code == 404


def test_decision_rejects_percent_risk_cap_above_100(client, db_session):
    # Regression test for the real P0 sizing bug: risk_cap=5000 with
    # risk_cap_is_percent=true previously sized a $10,000-budget decision
    # to a $500,000 max loss instead of being rejected outright.
    from models.company import Company

    db_session.add(Company(ticker="ZZDECRISK", name="ZZ Decision Risk Cap Co", cik="0009999931"))
    db_session.flush()

    response = client.post(
        "/api/v1/research/zzdecrisk/decision",
        json={"trade_budget": "10000", "risk_cap": "5000", "risk_cap_is_percent": True},
    )

    assert response.status_code == 422
    assert "risk_cap_is_percent" in response.json()["error"]


def test_decision_generates_from_stub_llm(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZDECAPI", name="ZZ Decision API Co", cik="0009999930"))
    db_session.flush()

    response = client.post("/api/v1/research/zzdecapi/decision")

    assert response.status_code == 200
    body = response.json()
    assert body["direction"] == "bullish"
    assert body["volatility_view"] == "long_vol"
    assert body["decision_source"] == "ai"
    assert body["status"] == "open"
    assert body["is_final"] is False
    assert 0 <= body["confidence_score"] <= 100
    assert "not investment advice" in body["disclaimer"]


def test_decision_generation_persists_a_real_row(client, db_session):
    from models.ai_decision_version import AIDecisionVersion
    from models.company import Company

    company = Company(ticker="ZZDECVER", name="ZZ Decision Version Co", cik="0009999931")
    db_session.add(company)
    db_session.flush()

    response = client.post("/api/v1/research/zzdecver/decision")
    assert response.status_code == 200

    rows = (
        db_session.query(AIDecisionVersion)
        .filter(AIDecisionVersion.company_id == company.id)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].provider == "stub"


def test_decision_generating_twice_creates_two_versions_never_overwrites(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZDECV2", name="ZZ Decision Version Co 2", cik="0009999932"))
    db_session.flush()

    first = client.post("/api/v1/research/zzdecv2/decision")
    second = client.post("/api/v1/research/zzdecv2/decision")
    assert first.status_code == 200
    assert second.status_code == 200

    versions = client.get("/api/v1/research/zzdecv2/decisions").json()
    assert len(versions) == 2
    assert versions[0]["id"] != versions[1]["id"]


def test_decision_manual_override_records_manual_source(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZDECOV", name="ZZ Decision Override Co", cik="0009999933"))
    db_session.flush()

    response = client.post(
        "/api/v1/research/zzdecov/decision",
        json={"direction": "bearish", "volatility_view": "short_vol"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["direction"] == "bearish"
    assert body["volatility_view"] == "short_vol"
    assert body["decision_source"] == "manual_override"


def test_decision_history_unknown_ticker_returns_404(client):
    response = client.get("/api/v1/research/ZZNODECHIST/decisions")
    assert response.status_code == 404


def test_decision_can_be_fetched_by_id(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZDECFETCH", name="ZZ Decision Fetch Co", cik="0009999934"))
    db_session.flush()

    client.post("/api/v1/research/zzdecfetch/decision")
    versions = client.get("/api/v1/research/zzdecfetch/decisions").json()
    decision_id = versions[0]["id"]

    response = client.get(f"/api/v1/research/zzdecfetch/decisions/{decision_id}")
    assert response.status_code == 200
    assert response.json()["direction"] == "bullish"


def test_decision_for_a_different_company_returns_404(client, db_session):
    from models.company import Company

    company_a = Company(ticker="ZZDECA", name="ZZ Decision Co A", cik="0009999935")
    company_b = Company(ticker="ZZDECB", name="ZZ Decision Co B", cik="0009999936")
    db_session.add_all([company_a, company_b])
    db_session.flush()

    client.post("/api/v1/research/zzdeca/decision")
    versions_a = client.get("/api/v1/research/zzdeca/decisions").json()
    decision_id = versions_a[0]["id"]

    response = client.get(f"/api/v1/research/zzdecb/decisions/{decision_id}")
    assert response.status_code == 404


def test_decision_can_be_deleted(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZDECDEL", name="ZZ Decision Delete Co", cik="0009999937"))
    db_session.flush()

    client.post("/api/v1/research/zzdecdel/decision")
    versions = client.get("/api/v1/research/zzdecdel/decisions").json()
    decision_id = versions[0]["id"]

    delete_response = client.delete(f"/api/v1/research/zzdecdel/decisions/{decision_id}")
    assert delete_response.status_code == 204

    get_response = client.get(f"/api/v1/research/zzdecdel/decisions/{decision_id}")
    assert get_response.status_code == 404


def test_decision_mark_final_unmarks_prior_final(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZDECFIN", name="ZZ Decision Final Co", cik="0009999938"))
    db_session.flush()

    client.post("/api/v1/research/zzdecfin/decision")
    client.post("/api/v1/research/zzdecfin/decision")
    versions = client.get("/api/v1/research/zzdecfin/decisions").json()
    newest_id = versions[0]["id"]
    oldest_id = versions[1]["id"]

    client.post(f"/api/v1/research/zzdecfin/decisions/{oldest_id}/final")
    response = client.post(f"/api/v1/research/zzdecfin/decisions/{newest_id}/final")

    assert response.status_code == 200
    assert response.json()["is_final"] is True
    oldest = client.get(f"/api/v1/research/zzdecfin/decisions/{oldest_id}").json()
    assert oldest["is_final"] is False


def test_decision_settle_is_a_noop_without_real_post_earnings_data(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZDECSETL", name="ZZ Decision Settle Co", cik="0009999939"))
    db_session.flush()

    client.post("/api/v1/research/zzdecsetl/decision")
    versions = client.get("/api/v1/research/zzdecsetl/decisions").json()
    decision_id = versions[0]["id"]
    client.post(f"/api/v1/research/zzdecsetl/decisions/{decision_id}/final")

    response = client.post(f"/api/v1/research/zzdecsetl/decisions/{decision_id}/settle")

    assert response.status_code == 200
    body = response.json()
    assert body["settled"] is False
    assert body["message"]  # a real, specific reason -- never a silent no-op
    assert body["decision"]["status"] == "open"  # no real earnings event yet -- untouched


def test_decision_settle_reports_not_final_before_final_decision_is_marked(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZDECNF", name="ZZ Decision Not Final Co", cik="0009999940"))
    db_session.flush()

    client.post("/api/v1/research/zzdecnf/decision")
    versions = client.get("/api/v1/research/zzdecnf/decisions").json()
    decision_id = versions[0]["id"]

    response = client.post(f"/api/v1/research/zzdecnf/decisions/{decision_id}/settle")

    assert response.status_code == 200
    body = response.json()
    assert body["settled"] is False
    assert "final" in body["message"].lower()
    assert body["decision"]["settlement_state"] == "not_final"
    assert body["decision"]["settlement_eligible"] is False


def test_pending_final_decisions_lists_final_open_decisions_across_companies(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZDECPEND", name="ZZ Decision Pending Co", cik="0009999941"))
    db_session.flush()

    before = client.get("/api/v1/research/decisions/pending").json()
    before_pending_ids = {p["decision"]["id"] for p in before["pending"]}

    client.post("/api/v1/research/zzdecpend/decision")
    versions = client.get("/api/v1/research/zzdecpend/decisions").json()
    decision_id = versions[0]["id"]

    # Not final yet -- must not appear in the pending list.
    mid = client.get("/api/v1/research/decisions/pending").json()
    assert decision_id not in {p["decision"]["id"] for p in mid["pending"]}

    client.post(f"/api/v1/research/zzdecpend/decisions/{decision_id}/final")

    after = client.get("/api/v1/research/decisions/pending").json()
    after_pending_ids = {p["decision"]["id"] for p in after["pending"]}
    assert decision_id in after_pending_ids
    assert after["final_count"] == before["final_count"] + 1
    assert after["pending_count"] == len(after["pending"])
    row = next(p for p in after["pending"] if p["decision"]["id"] == decision_id)
    assert row["ticker"] == "ZZDECPEND"
    assert before_pending_ids <= after_pending_ids


def test_track_record_with_no_settled_decisions_returns_honest_empty_state(client):
    response = client.get("/api/v1/research/track-record")

    assert response.status_code == 200
    body = response.json()
    assert body["evaluated_count"] == 0
    assert body["directional_accuracy"]["total"] == 0
    assert body["directional_accuracy"]["pct"] is None
    assert body["strategy_win_rate_available"] is False


def test_portfolio_positions_filtered_by_ticker(client, db_session):
    from datetime import UTC, datetime
    from decimal import Decimal

    from models.portfolio_position_snapshot import PortfolioPositionSnapshot

    now = datetime.now(UTC)
    db_session.add_all(
        [
            PortfolioPositionSnapshot(
                account_id_masked="U12****34",
                snapshot_timestamp=now,
                conid=1,
                contract_description="ZZPORT",
                asset_class="STK",
                quantity=Decimal("10"),
                source_provider="test",
                retrieved_at=now,
            ),
            PortfolioPositionSnapshot(
                account_id_masked="U12****34",
                snapshot_timestamp=now,
                conid=2,
                contract_description="OTHR",
                asset_class="STK",
                quantity=Decimal("5"),
                source_provider="test",
                retrieved_at=now,
            ),
        ]
    )
    db_session.flush()

    response = client.get("/api/v1/portfolio/positions", params={"ticker": "zzport"})

    assert response.status_code == 200
    body = response.json()
    assert len(body["positions"]) == 1
    assert body["positions"][0]["contract_description"] == "ZZPORT"
