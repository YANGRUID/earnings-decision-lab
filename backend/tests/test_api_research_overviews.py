"""GET /research/overviews -- the Company Search bulk read (V4-only reset,
2026-09-02). One request for every researched company, persisted state only:
it must never reach a market-data provider and never write."""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from models.company import Company
from models.volatility_snapshot import VolatilitySnapshot


@pytest.fixture(scope="module")
def test_client() -> Iterator[TestClient]:
    from api.main import app

    with TestClient(app) as client:
        yield client


@pytest.fixture
def client(test_client, db_session) -> Iterator[TestClient]:
    from api.deps import get_db

    test_client.app.dependency_overrides[get_db] = lambda: db_session
    yield test_client
    test_client.app.dependency_overrides.clear()


def test_lists_every_company_in_one_request_without_touching_a_provider(
    client, db_session, monkeypatch
):
    for ticker, name in (("ZZA", "Zeta A"), ("ZZB", "Zeta B")):
        db_session.add(Company(ticker=ticker, name=name, cik=None, sector=None, exchange="NASDAQ"))
    db_session.flush()

    def _boom(*_a, **_k):  # pragma: no cover - must never be called
        raise AssertionError("the bulk overview reached a market-data provider")

    import api.routers.research as research_router

    monkeypatch.setattr(research_router, "get_options_provider", _boom)
    monkeypatch.setattr(research_router, "compute_and_persist_volatility_snapshot", _boom)
    before = db_session.query(VolatilitySnapshot).count()

    response = client.get("/api/v1/research/overviews")

    assert response.status_code == 200
    tickers = [o["ticker"] for o in response.json()["overviews"]]
    assert tickers == sorted(tickers)
    assert {"ZZA", "ZZB"} <= set(tickers)
    row = next(o for o in response.json()["overviews"] if o["ticker"] == "ZZA")
    assert row["company"]["name"] == "Zeta A"
    assert row["latest_job"] is None
    assert row["options_market"]["chain_exists"] is False
    assert db_session.query(VolatilitySnapshot).count() == before


def test_literal_overviews_path_is_not_shadowed_by_symbol_routes(client):
    # /research/overviews must resolve to the list, never to a symbol lookup.
    assert client.get("/api/v1/research/overviews").status_code == 200
