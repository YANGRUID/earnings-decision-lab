"""Serves the most recent evaluation run (see docs/evaluation.md and
evaluation/scripts/run_all.py). Results are real output from real runs
against the real database and LLM provider, written to
evaluation/results/latest.json -- gitignored, since a raw JSON blob isn't
the record of truth (docs/evaluation.md, committed, is), and regenerating
it silently on every commit would risk it drifting out of sync with what
the docs actually claim. A fresh clone or CI environment has no results
file at all, which this endpoint represents honestly rather than erroring.
"""

import json
from pathlib import Path

from fastapi import APIRouter

from evaluation.models import EvaluationRun
from schemas.api import EvaluationStatusResponse

router = APIRouter(prefix="/evaluations", tags=["evaluations"])

_RESULTS_PATH = Path(__file__).resolve().parents[4] / "evaluation" / "results" / "latest.json"


@router.get("/latest", response_model=EvaluationStatusResponse)
def latest_evaluation() -> EvaluationStatusResponse:
    if not _RESULTS_PATH.exists():
        return EvaluationStatusResponse(available=False, run=None)
    run = EvaluationRun.model_validate(json.loads(_RESULTS_PATH.read_text()))
    return EvaluationStatusResponse(available=True, run=run)
