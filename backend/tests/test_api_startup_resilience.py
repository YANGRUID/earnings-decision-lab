"""Regression test for a real bug: the API's lifespan used to call
get_llm_provider() unconditionally at startup, so with no LLM provider
configured (exactly CI's situation — no real key is ever set there, by
design) the whole app failed to start, taking down /health and every
other endpoint along with the AI ones. Reproduces that exact scenario
directly against a fresh app instance (not the module-scoped, real-key
app the rest of test_api.py uses) to make sure it can't silently regress.
"""

from fastapi.testclient import TestClient

from core.config import Settings
from models.document_chunk import EMBEDDING_DIM
from rag.embeddings import EmbeddingProvider


class _StubEmbedder(EmbeddingProvider):
    model_name = "stub"
    dimension = EMBEDDING_DIM

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * (EMBEDDING_DIM - 1) for _ in texts]


def test_app_starts_and_serves_data_endpoints_without_any_llm_configured(monkeypatch):
    import api.deps as deps_module
    import api.main as main_module

    no_key_settings = Settings(_env_file=None, llm_provider="deepseek")
    monkeypatch.setattr(main_module, "get_settings", lambda: no_key_settings)
    # api.deps.get_llm constructs the LLM provider fresh per-request (see
    # services/provider_settings.py) from its own get_settings import, not
    # main.py's -- must be patched separately, or this test would pick up
    # whatever real DEEPSEEK_API_KEY happens to be in this machine's real
    # .env via the process-wide lru_cache'd Settings, defeating the point
    # of this no-key regression test.
    monkeypatch.setattr(deps_module, "get_settings", lambda: no_key_settings)
    # Only the missing-LLM-key scenario is under test here; the embedder is
    # unrelated to this bug and stubbed at its construction site (lifespan
    # calls FastEmbedProvider() directly, not through a dependency-overridable
    # function) to avoid a real HuggingFace download in CI, consistent with
    # this project's no-live-network-calls-in-tests policy.
    monkeypatch.setattr(main_module, "FastEmbedProvider", _StubEmbedder)

    app = main_module.create_app()
    with TestClient(app) as client:
        health = client.get("/api/v1/health")
        assert health.status_code == 200

        companies = client.get("/api/v1/companies")
        assert companies.status_code == 200


        research = client.post("/api/v1/research/query", json={"question": "anything"})
        assert research.status_code == 503
        assert "AI service unavailable" in research.json()["error"]
