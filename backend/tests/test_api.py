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


def test_strategy_lab_no_volatility_snapshot_returns_honest_empty_state(client, db_session):
    from models.company import Company

    db_session.add(Company(ticker="ZZSLAB", name="ZZ Strategy Lab Co", cik="0009999915"))
    db_session.flush()

    response = client.get("/api/v1/research/zzslab/strategies")

    assert response.status_code == 200
    body = response.json()
    assert body["strategies"] == []
    assert body["expiration"] is None


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
    top = body["strategies"][0]
    assert top["rank"] == 1
    assert "ZZSLAB2" in top["explanation"]


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
