"""Shared setup for the evaluation runner scripts: puts backend/src on
sys.path (these scripts live outside the backend package, unlike its own
tests, which get this via pytest's ``pythonpath`` setting) and provides
small helpers for loading datasets and constructing a DB session / LLM /
embedder the same way the FastAPI app does.

Run scripts from the backend directory so they pick up backend's own
dependencies and .env, e.g.:

    cd backend && uv run python ../evaluation/scripts/run_all.py
"""

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
DATASETS_DIR = REPO_ROOT / "evaluation" / "datasets"
RESULTS_DIR = REPO_ROOT / "evaluation" / "results"

if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))


def load_jsonl(name: str) -> list[dict]:
    path = DATASETS_DIR / name
    with path.open() as f:
        return [json.loads(line) for line in f if line.strip()]


def get_db_session():
    from db.session import SessionLocal

    return SessionLocal()


def get_llm_and_embedder():
    from core.config import get_settings
    from rag.embeddings import FastEmbedProvider
    from services.llm.factory import get_llm_provider

    settings = get_settings()
    llm = get_llm_provider(settings)
    embedder = FastEmbedProvider()
    return llm, embedder
