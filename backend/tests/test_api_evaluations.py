"""GET /api/v1/evaluations/latest reads a real file off disk
(evaluation/results/latest.json) rather than a DB table -- these tests
monkeypatch the module-level path constant instead of seeding data.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from evaluation.models import EvaluationRun, RetrievalItemResult, RetrievalSummary


@pytest.fixture(scope="module")
def test_client() -> Iterator[TestClient]:
    from api.main import app

    with TestClient(app) as client:
        yield client


def _sample_run() -> EvaluationRun:
    return EvaluationRun(
        run_at=datetime.now(UTC),
        llm_provider="deepseek",
        llm_model="deepseek-v4-flash",
        embedding_model="BAAI/bge-small-en-v1.5",
        retrieval=RetrievalSummary(
            item_count=1,
            mean_recall_at_3=1.0,
            mean_recall_at_5=1.0,
            mean_recall_at_10=1.0,
            mean_mrr=1.0,
            items=[
                RetrievalItemResult(
                    id="ret-01", query="test", recall_at_3=1.0, recall_at_5=1.0,
                    recall_at_10=1.0, mrr=1.0, retrieved_count=5,
                )
            ],
        ),
    )


def test_latest_evaluation_honest_empty_when_no_file(test_client, tmp_path, monkeypatch):
    from api.routers import evaluations as evaluations_module

    monkeypatch.setattr(evaluations_module, "_RESULTS_PATH", tmp_path / "does-not-exist.json")
    response = test_client.get("/api/v1/evaluations/latest")
    assert response.status_code == 200
    assert response.json() == {"available": False, "run": None}


def test_latest_evaluation_returns_real_run_when_present(test_client, tmp_path, monkeypatch):
    from api.routers import evaluations as evaluations_module

    results_path = tmp_path / "latest.json"
    results_path.write_text(_sample_run().model_dump_json())
    monkeypatch.setattr(evaluations_module, "_RESULTS_PATH", results_path)

    response = test_client.get("/api/v1/evaluations/latest")
    assert response.status_code == 200
    body = response.json()
    assert body["available"] is True
    assert body["run"]["llm_provider"] == "deepseek"
    assert body["run"]["retrieval"]["mean_recall_at_5"] == 1.0
