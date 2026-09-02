"""Deterministic fixture seeding for the Playwright E2E suite
(frontend/e2e/). Run against a disposable/test Postgres via
``DATABASE_URL`` -- never against a real deployment's database.

Creates ticker ZZE2E1 with a real (fabricated-but-internally-consistent)
options market: an underlying price, an upcoming earnings date, three
real candidate expirations at 2/9/23 days after that date with
deliberately different liquidity/DTE profiles (so the Expiration
Selection Engine's Auto pick is deterministic and testably different
from "nearest"), and 24 historical earnings-day reactions (so
Historical Compatibility / Estimated Probability have a real N >= 20
sample, clear of probability.py's LOW_SAMPLE_THRESHOLD).

Never depends on IBKR or any live provider -- every row here is a
direct DB insert, matching the same pattern the backend pytest suite
already uses for market-data fixtures (see tests/test_api.py).
"""

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import text

from db.session import SessionLocal
from models.company import Company
from models.earnings_estimate_snapshot import EarningsEstimateSnapshot
from models.earnings_event import EarningsEvent
from models.enums import OptionType, UpcomingEarningsDateSource
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.price_reaction import PriceReaction

TICKER = "ZZE2E1"
CIK = "0005550001"
UNDERLYING_PRICE = Decimal("100")

# Every table that can end up referencing this fixture's company_id,
# directly or (via earnings_event_id/filing_id) transitively -- in an
# order safe for plain DELETE under real FK constraints. New tables that
# start referencing company.id later must be added here too, or a
# re-seed will fail with a FK violation exactly like the one this list
# exists to prevent.
_DEPENDENT_TABLES_BY_COMPANY_ID = (
    "document_chunk",
    "ai_extraction",
    "ai_thesis_version",
    "volatility_snapshot",
    "research_preparation_job",
    "ai_research_query",
    "filing",
)


def _clear_existing(db, company: Company | None) -> None:
    if company is None:
        return
    for table in _DEPENDENT_TABLES_BY_COMPANY_ID:
        db.execute(text(f"DELETE FROM {table} WHERE company_id = :cid"), {"cid": company.id})
    db.execute(
        text(
            "DELETE FROM earnings_expectation_snapshot WHERE earnings_event_id IN "
            "(SELECT id FROM earnings_event WHERE company_id = :cid)"
        ),
        {"cid": company.id},
    )
    db.execute(
        text(
            "DELETE FROM earnings_result WHERE earnings_event_id IN "
            "(SELECT id FROM earnings_event WHERE company_id = :cid)"
        ),
        {"cid": company.id},
    )
    db.execute(
        text(
            "DELETE FROM price_reaction WHERE earnings_event_id IN "
            "(SELECT id FROM earnings_event WHERE company_id = :cid)"
        ),
        {"cid": company.id},
    )
    db.execute(text("DELETE FROM earnings_event WHERE company_id = :cid"), {"cid": company.id})
    db.query(EarningsEstimateSnapshot).filter_by(company_id=company.id).delete()
    db.query(OptionsSnapshot).filter_by(company_id=company.id).delete()
    db.query(PriceBar).filter_by(company_id=company.id).delete()
    db.delete(company)
    db.flush()


def _seed_chain(
    db, company: Company, snapshot_ts: datetime, expiration: date, quality: str
) -> None:
    """quality: "good" (full bid/ask coverage) or "thin" (mostly unpriced)."""
    strikes = [Decimal("95"), Decimal("100"), Decimal("105")]
    for strike in strikes:
        for option_type in (OptionType.CALL, OptionType.PUT):
            priced = quality == "good"
            db.add(
                OptionsSnapshot(
                    company_id=company.id,
                    snapshot_timestamp=snapshot_ts,
                    expiration_date=expiration,
                    strike=strike,
                    option_type=option_type,
                    bid=Decimal("2.00") if priced else None,
                    ask=Decimal("2.20") if priced else None,
                    implied_volatility=Decimal("0.45"),
                    open_interest=250 if priced else 0,
                    volume=80 if priced else 0,
                    source_provider="e2e_fixture",
                    retrieved_at=snapshot_ts,
                )
            )


def main() -> None:
    db = SessionLocal()
    try:
        existing = db.query(Company).filter(Company.ticker == TICKER).one_or_none()
        _clear_existing(db, existing)

        company = Company(ticker=TICKER, name="ZZ E2E Fixture Co", cik=CIK)
        db.add(company)
        db.flush()

        now = datetime.now(UTC)
        today = now.date()

        db.add(
            PriceBar(
                ticker=TICKER,
                company_id=company.id,
                trade_date=today,
                source_provider="e2e_fixture",
                open=UNDERLYING_PRICE,
                high=UNDERLYING_PRICE + 1,
                low=UNDERLYING_PRICE - 1,
                close=UNDERLYING_PRICE,
                volume=1_000_000,
                retrieved_at=now,
            )
        )

        earnings_date = today + timedelta(days=10)
        db.add(
            EarningsEstimateSnapshot(
                company_id=company.id,
                fiscal_period_end_date=earnings_date - timedelta(days=45),
                horizon="fiscal quarter",
                snapshot_timestamp=now,
                estimated_report_date=earnings_date,
                date_source=UpcomingEarningsDateSource.ALPHA_VANTAGE,
                source_provider="e2e_fixture",
                retrieved_at=now,
            )
        )

        # Three real post-earnings candidates with deliberately different
        # DTE/liquidity so the Expiration Selection Engine's score-driven
        # Auto pick is verifiably NOT just "nearest": near_expiration (2
        # days after earnings) has full liquidity but a poor DTE-suitability
        # score; sweet_spot_expiration (9 days after) has full liquidity AND
        # a good DTE score, so it should win; far_expiration (23 days
        # after) has full liquidity but a worse event-fit score from excess
        # time premium.
        near_expiration = earnings_date + timedelta(days=2)
        sweet_spot_expiration = earnings_date + timedelta(days=9)
        far_expiration = earnings_date + timedelta(days=23)
        _seed_chain(db, company, now, near_expiration, quality="good")
        _seed_chain(db, company, now, sweet_spot_expiration, quality="good")
        _seed_chain(db, company, now, far_expiration, quality="good")

        # 24 historical earnings-day reactions -- N >= 20, clear of the
        # low-sample-confidence threshold, with a realistic spread of
        # signed moves so move-compatibility/probability math has a real
        # distribution to work with (never a single repeated fixed value).
        signed_moves = [
            "0.062",
            "-0.041",
            "0.089",
            "-0.075",
            "0.033",
            "-0.028",
            "0.104",
            "-0.056",
            "0.047",
            "-0.019",
            "0.071",
            "-0.083",
            "0.038",
            "-0.052",
            "0.096",
            "-0.031",
            "0.058",
            "-0.044",
            "0.067",
            "-0.037",
            "0.081",
            "-0.025",
            "0.049",
            "-0.061",
        ]
        for i, move in enumerate(signed_moves):
            fiscal_quarter = (i % 4) + 1
            fiscal_year = 2020 + (i // 4)
            event = EarningsEvent(
                company_id=company.id,
                fiscal_year=fiscal_year,
                fiscal_quarter=fiscal_quarter,
                earnings_date=date(fiscal_year, fiscal_quarter * 3, 10),
            )
            db.add(event)
            db.flush()
            db.add(
                PriceReaction(
                    earnings_event_id=event.id,
                    next_day_move_pct=Decimal(move),
                    source_provider="e2e_fixture",
                    retrieved_at=now,
                )
            )

        db.commit()
        print(
            f"Seeded {TICKER}: company_id={company.id}, earnings_date={earnings_date}, "
            f"expirations=[{near_expiration}, {sweet_spot_expiration}, {far_expiration}], "
            f"{len(signed_moves)} historical events."
        )

        # Every date above is computed relative to "today" (real, by
        # design -- a fixture anchored to an absolute date would eventually
        # land in the past and stop being a valid earnings/expiration
        # scenario). Spec files must never hardcode the resulting absolute
        # dates -- that only matches on the day someone happened to run
        # this script and silently breaks every day after (confirmed live:
        # this is exactly what broke options-decision-engine.spec.ts before
        # this was added). Written only when a caller (global-setup.ts)
        # asks for it via this env var, so running the script standalone
        # for local DB seeding doesn't require a frontend checkout at all.
        dates_path = os.environ.get("E2E_FIXTURE_DATES_PATH")
        if dates_path:
            with open(dates_path, "w") as f:
                json.dump(
                    {
                        "earnings_date": earnings_date.isoformat(),
                        "near_expiration": near_expiration.isoformat(),
                        "sweet_spot_expiration": sweet_spot_expiration.isoformat(),
                        "far_expiration": far_expiration.isoformat(),
                    },
                    f,
                )
    finally:
        db.close()


if __name__ == "__main__":
    main()
