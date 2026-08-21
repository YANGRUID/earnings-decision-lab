"""Phase 4.6 -- read-only endpoints over the AI Earnings Analyst Track
Record: portfolio performance, prediction accuracy, and probability
calibration, computed live from the real, immutable Phase 4 tables. No
mutation endpoint exists, or ever will -- this router only ever reads
DecisionSnapshot/EntrySnapshot/EntryCaptureAttempt/
SettlementCaptureAttempt/BenchmarkPortfolio/VolatilitySnapshot and
computes in memory (services/benchmark_track_record.py), matching the
same standing decision every other Phase 4 router has made since
api/routers/decision_snapshots.py's own docstring first stated it.
"""

from fastapi import APIRouter, Query

from analytics.decision.track_record_math import Rate
from api.deps import DbSession
from api.exceptions import NotFoundError
from models.enums import RiskProfile
from schemas.api import (
    BenchmarkCalibrationBucketResponse,
    BenchmarkCalibrationResponse,
    BenchmarkTrackRecordResponse,
    RateResponse,
)
from services.benchmark_track_record import (
    CalibrationBucket,
    TrackRecordFilters,
    compute_benchmark_calibration,
    compute_benchmark_track_record,
    resolve_portfolio,
)

router = APIRouter(prefix="/benchmark", tags=["benchmark"])


def _rate_response(rate: Rate) -> RateResponse:
    return RateResponse(correct=rate.correct, total=rate.total, pct=rate.pct)


def _bucket_response(bucket: CalibrationBucket) -> BenchmarkCalibrationBucketResponse:
    return BenchmarkCalibrationBucketResponse(
        label=bucket.label, lower=bucket.lower, upper=bucket.upper, rate=_rate_response(bucket.rate)
    )


@router.get("/track-record", response_model=BenchmarkTrackRecordResponse)
def get_benchmark_track_record(
    db: DbSession,
    portfolio_id: int | None = None,
    strategy: str | None = None,
    confidence_bucket: str | None = Query(
        default=None, pattern="^(<60%|60-70%|70-80%|80-90%|90%\\+)$"
    ),
    dte_bucket: str | None = Query(default=None, pattern="^(0-3|4-7|8-14|15-30|30\\+)$"),
    risk_profile: RiskProfile | None = None,
    iv_regime: str | None = None,
) -> BenchmarkTrackRecordResponse:
    portfolio = resolve_portfolio(db, portfolio_id)
    if portfolio is None:
        raise NotFoundError("no active benchmark portfolio found")

    summary = compute_benchmark_track_record(
        db,
        portfolio,
        TrackRecordFilters(
            strategy=strategy,
            confidence_bucket=confidence_bucket,
            dte_bucket=dte_bucket,
            risk_profile=risk_profile,
            iv_regime=iv_regime,
        ),
    )
    return BenchmarkTrackRecordResponse(
        portfolio_id=summary.portfolio_id,
        total_decisions=summary.total_decisions,
        settled_decisions=summary.settled_decisions,
        win_rate=_rate_response(summary.win_rate),
        average_r=summary.average_r,
        median_r=summary.median_r,
        expectancy=summary.expectancy,
        profit_factor=summary.profit_factor,
        max_drawdown=summary.max_drawdown,
        max_drawdown_pct=summary.max_drawdown_pct,
        directional_accuracy=_rate_response(summary.directional_accuracy),
        breakeven_accuracy=_rate_response(summary.breakeven_accuracy),
        range_accuracy=_rate_response(summary.range_accuracy),
    )


@router.get("/calibration", response_model=BenchmarkCalibrationResponse)
def get_benchmark_calibration(
    db: DbSession, portfolio_id: int | None = None
) -> BenchmarkCalibrationResponse:
    portfolio = resolve_portfolio(db, portfolio_id)
    if portfolio is None:
        raise NotFoundError("no active benchmark portfolio found")

    summary = compute_benchmark_calibration(db, portfolio)
    return BenchmarkCalibrationResponse(
        portfolio_id=summary.portfolio_id,
        settled_decisions=summary.settled_decisions,
        buckets=[_bucket_response(bucket) for bucket in summary.buckets],
    )
