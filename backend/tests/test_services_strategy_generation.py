from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.options.strategy_candidates import StrategyCategory
from models.company import Company
from models.enums import OptionType
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from services.strategy_generation import generate_strategy_candidates

SNAPSHOT_TS = datetime(2026, 9, 15, 15, 0, tzinfo=UTC)
EARNINGS_DATE = date(2026, 9, 22)
NEAR_EXP = date(2026, 9, 26)
FAR_PAST_EXP = date(2026, 9, 19)  # before earnings -- must never be used


def _seed_company(db_session, ticker: str = "ZZSGEN") -> Company:
    company = Company(ticker=ticker, name="ZZ Strategy Gen Co", cik="0009999930")
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
            source_provider="test",
            retrieved_at=snapshot_timestamp,
        )
    )
    db_session.flush()


def _seed_full_chain(db_session, company: Company) -> None:
    for strike in (Decimal("95"), Decimal("100"), Decimal("105"), Decimal("110")):
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=strike,
            option_type=OptionType.CALL,
            bid=Decimal("1.90"),
            ask=Decimal("2.10"),
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=strike,
            option_type=OptionType.PUT,
            bid=Decimal("1.90"),
            ask=Decimal("2.10"),
        )


def test_returns_empty_when_no_options_data_ingested(db_session):
    company = _seed_company(db_session)
    assert generate_strategy_candidates(db_session, company, EARNINGS_DATE) == []


def test_returns_empty_when_no_expiration_after_earnings_date(db_session):
    company = _seed_company(db_session)
    _seed_price_bar(db_session, company.ticker, date(2026, 9, 12), Decimal("100"))
    _seed_option_quote(
        db_session,
        company,
        expiration=FAR_PAST_EXP,
        strike=Decimal("100"),
        option_type=OptionType.CALL,
        bid=Decimal("1.90"),
        ask=Decimal("2.10"),
    )
    assert generate_strategy_candidates(db_session, company, EARNINGS_DATE) == []


def test_returns_empty_when_no_underlying_price_on_record(db_session):
    company = _seed_company(db_session)
    _seed_full_chain(db_session, company)
    assert generate_strategy_candidates(db_session, company, EARNINGS_DATE) == []


def test_real_chain_and_price_produce_real_candidates(db_session):
    company = _seed_company(db_session)
    _seed_price_bar(db_session, company.ticker, date(2026, 9, 12), Decimal("100"))
    _seed_full_chain(db_session, company)

    candidates = generate_strategy_candidates(db_session, company, EARNINGS_DATE)

    assert candidates != []
    assert all(c.expiration == NEAR_EXP for c in candidates)
    assert all(c.underlying_price == Decimal("100") for c in candidates)
    long_call = next(c for c in candidates if c.category == StrategyCategory.LONG_CALL)
    assert long_call.legs[0].strike == Decimal("100")


def test_uses_only_the_latest_snapshot_not_stale_ones(db_session):
    company = _seed_company(db_session)
    _seed_price_bar(db_session, company.ticker, date(2026, 9, 12), Decimal("100"))
    stale_ts = SNAPSHOT_TS.replace(day=8)
    # A stale, earlier snapshot with a *different* expiration -- must be
    # ignored in favor of the latest one.
    _seed_option_quote(
        db_session,
        company,
        expiration=date(2026, 9, 30),
        strike=Decimal("100"),
        option_type=OptionType.CALL,
        bid=Decimal("1.90"),
        ask=Decimal("2.10"),
        snapshot_timestamp=stale_ts,
    )
    _seed_full_chain(db_session, company)

    candidates = generate_strategy_candidates(db_session, company, EARNINGS_DATE)

    assert candidates != []
    assert all(c.expiration == NEAR_EXP for c in candidates)
