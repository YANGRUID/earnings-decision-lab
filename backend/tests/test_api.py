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

        if schema is IntentClassification:
            return IntentClassification(category=IntentCategory.GENERAL, reasoning="stub")
        if schema is VerificationResult:
            return VerificationResult(supported=True)
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


def test_list_companies_returns_real_seeded_companies(client):
    response = client.get("/api/v1/companies")
    assert response.status_code == 200
    tickers = {c["ticker"] for c in response.json()}
    assert {"NVDA", "AMD", "MU", "SNDK"} <= tickers


def test_get_company_not_found_returns_404_with_request_id(client):
    response = client.get("/api/v1/companies/NOSUCHTICKER")
    assert response.status_code == 404
    body = response.json()
    assert "not found" in body["error"]
    assert body["request_id"] is not None


def test_list_earnings_filtered_by_ticker(client):
    response = client.get("/api/v1/earnings", params={"ticker": "MU", "limit": 3})
    assert response.status_code == 200
    events = response.json()
    assert len(events) <= 3
    assert len(events) > 0


def test_get_earnings_event_detail_includes_nested_company_and_result(client):
    list_response = client.get("/api/v1/earnings", params={"ticker": "MU", "limit": 1})
    event_id = list_response.json()[0]["id"]

    response = client.get(f"/api/v1/earnings/{event_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["company"]["ticker"] == "MU"


def test_get_earnings_event_not_found(client):
    response = client.get("/api/v1/earnings/99999999")
    assert response.status_code == 404


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
