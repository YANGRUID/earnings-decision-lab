from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from models.company import Company
from models.enums import OptionType
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.volatility_snapshot import VolatilitySnapshot
from services.options_analytics import (
    compute_and_persist_volatility_snapshot,
    get_latest_volatility_snapshot,
)

SNAPSHOT_TS = datetime(2025, 9, 15, 15, 0, tzinfo=UTC)
EARNINGS_DATE = date(2025, 9, 22)
NEAR_EXP = date(2025, 9, 26)
FAR_EXP = date(2025, 9, 19)  # before earnings -- must never be selected


def _seed_company(db_session, ticker: str = "ZZOPT1") -> Company:
    company = Company(ticker=ticker, name="ZZ Options Test Co", cik="0009999922")
    db_session.add(company)
    db_session.flush()
    return company


def _seed_price_bar(db_session, ticker: str, trade_date: date, close: Decimal) -> None:
    db_session.add(
        PriceBar(
            ticker=ticker,
            trade_date=trade_date,
            open=close,
            high=close,
            low=close,
            close=close,
            volume=1_000_000,
            source_provider="test",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.flush()


def _seed_option_quote(
    db_session,
    company: Company,
    *,
    expiration: date,
    strike: Decimal,
    option_type: OptionType,
    bid: Decimal,
    ask: Decimal,
    iv: Decimal | None = None,
    snapshot_timestamp: datetime = SNAPSHOT_TS,
) -> None:
    db_session.add(
        OptionsSnapshot(
            company_id=company.id,
            snapshot_timestamp=snapshot_timestamp,
            expiration_date=expiration,
            strike=strike,
            option_type=option_type,
            bid=bid,
            ask=ask,
            implied_volatility=iv,
            source_provider="test",
            retrieved_at=snapshot_timestamp,
        )
    )
    db_session.flush()


class TestComputeAndPersistVolatilitySnapshot:
    def test_returns_none_when_no_options_data_ingested(self, db_session):
        company = _seed_company(db_session)
        assert compute_and_persist_volatility_snapshot(db_session, company, EARNINGS_DATE) is None

    def test_returns_none_when_no_expiration_after_earnings_date(self, db_session):
        company = _seed_company(db_session)
        _seed_price_bar(db_session, company.ticker, date(2025, 9, 12), Decimal("114.50"))
        _seed_option_quote(
            db_session,
            company,
            expiration=FAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("4.20"),
            ask=Decimal("4.40"),
        )

        assert compute_and_persist_volatility_snapshot(db_session, company, EARNINGS_DATE) is None

    def test_returns_none_when_no_price_data_available(self, db_session):
        company = _seed_company(db_session)
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("4.20"),
            ask=Decimal("4.40"),
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.PUT,
            bid=Decimal("4.00"),
            ask=Decimal("4.20"),
        )

        assert compute_and_persist_volatility_snapshot(db_session, company, EARNINGS_DATE) is None

    def test_persists_real_implied_move_and_atm_iv(self, db_session):
        company = _seed_company(db_session)
        _seed_price_bar(db_session, company.ticker, date(2025, 9, 12), Decimal("114.50"))
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("4.20"),
            ask=Decimal("4.40"),
            iv=Decimal("0.50"),
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.PUT,
            bid=Decimal("4.00"),
            ask=Decimal("4.20"),
            iv=Decimal("0.52"),
        )
        # A quote at an earlier expiration on record too -- must be ignored
        # in favor of the nearest one strictly after the earnings date.
        _seed_option_quote(
            db_session,
            company,
            expiration=FAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("6.00"),
            ask=Decimal("6.20"),
        )

        row = compute_and_persist_volatility_snapshot(db_session, company, EARNINGS_DATE)

        assert row is not None
        assert row.near_term_expiration == NEAR_EXP
        assert row.implied_move_absolute == Decimal("8.40")
        expected_pct = (Decimal("8.40") / Decimal("114.50")).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        assert row.implied_move_pct == expected_pct
        assert row.atm_iv_near == Decimal("0.51")
        assert row.inputs["atm_iv_method"] == "average_of_call_and_put_iv"
        assert row.inputs["atm_iv_diverges"] is False
        assert row.snapshot_timestamp == SNAPSHOT_TS

        persisted = db_session.query(VolatilitySnapshot).filter_by(company_id=company.id).all()
        assert len(persisted) == 1

    def test_uses_most_recent_snapshot_timestamp_when_multiple_exist(self, db_session):
        company = _seed_company(db_session)
        older_ts = datetime(2025, 9, 8, 15, 0, tzinfo=UTC)
        _seed_price_bar(db_session, company.ticker, date(2025, 9, 12), Decimal("114.50"))
        _seed_price_bar(db_session, company.ticker, date(2025, 9, 5), Decimal("100.00"))

        # Older snapshot: no put at ATM strike, would raise if it were used.
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("100"),
            option_type=OptionType.CALL,
            bid=Decimal("1.00"),
            ask=Decimal("1.20"),
            snapshot_timestamp=older_ts,
        )
        # Latest snapshot: complete pair.
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("4.20"),
            ask=Decimal("4.40"),
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.PUT,
            bid=Decimal("4.00"),
            ask=Decimal("4.20"),
        )

        row = compute_and_persist_volatility_snapshot(db_session, company, EARNINGS_DATE)

        assert row is not None
        assert row.snapshot_timestamp == SNAPSHOT_TS


class TestGetLatestVolatilitySnapshot:
    def test_returns_none_when_no_snapshots_exist(self, db_session):
        company = _seed_company(db_session)
        assert get_latest_volatility_snapshot(db_session, company.id) is None

    def test_returns_most_recent_by_snapshot_timestamp(self, db_session):
        company = _seed_company(db_session)
        db_session.add(
            VolatilitySnapshot(
                company_id=company.id,
                snapshot_timestamp=datetime(2025, 9, 1, tzinfo=UTC),
                method="atm_straddle",
                computed_at=datetime.now(UTC),
            )
        )
        db_session.add(
            VolatilitySnapshot(
                company_id=company.id,
                snapshot_timestamp=SNAPSHOT_TS,
                method="atm_straddle",
                near_term_expiration=NEAR_EXP,
                computed_at=datetime.now(UTC),
            )
        )
        db_session.flush()

        latest = get_latest_volatility_snapshot(db_session, company.id)
        assert latest is not None
        assert latest.near_term_expiration == NEAR_EXP
