"""Phase 4.9 -- POST /admin/run-earnings-sync,
/admin/run-decision-generation, /admin/run-settlement-capture.

The underlying job functions (services/scheduler.py) each open their
own real SessionLocal() and commit independently of this test suite's
own rollback-wrapped db_session fixture -- calling them unmocked here
would write real, permanent rows into whatever database DATABASE_URL
points at (this project's tests run against a real local Postgres, not
a disposable one; see tests/conftest.py), and would make a real network
call to Finnhub/IBKR on every `pytest` run. Neither is acceptable for a
unit test, so every test here monkeypatches the job function itself and
verifies the router's own wiring (which function it calls, how it
computes before/after counts, the production gate) -- the job
functions' own real behavior already has its own test coverage
elsewhere (e.g. test_services_scheduler.py, test_services_decision_
pipeline.py).
"""

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


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




