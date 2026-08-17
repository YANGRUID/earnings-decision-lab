from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from models.company import Company
from models.earnings_event import EarningsEvent
from models.enums import OptionType
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.price_reaction import PriceReaction
from models.volatility_snapshot import VolatilitySnapshot
from providers.base import OptionsDataProvider
from providers.types import OptionQuote
from services.options_analytics import (
    collect_forward_options_snapshot,
    compute_and_persist_volatility_snapshot,
    get_implied_vs_realized_moves,
    get_latest_volatility_snapshot,
)

SNAPSHOT_TS = datetime(2025, 9, 15, 15, 0, tzinfo=UTC)
EARNINGS_DATE = date(2025, 9, 22)
NEAR_EXP = date(2025, 9, 26)
FAR_EXP = date(2025, 9, 19)  # before earnings -- must never be selected


class _StubOptionsProvider(OptionsDataProvider):
    def __init__(self, quotes: list[OptionQuote]) -> None:
        self._quotes = quotes
        self.call_count = 0

    def get_option_chain(self, ticker, as_of, expiration=None, reference_date=None):
        self.call_count += 1
        return self._quotes


def _stub_quote(ticker: str, strike: Decimal, option_type: str) -> OptionQuote:
    now = datetime.now(UTC)
    return OptionQuote(
        ticker=ticker,
        snapshot_timestamp=now,
        expiration_date=NEAR_EXP,
        strike=strike,
        option_type=option_type,
        bid=Decimal("4.00"),
        ask=Decimal("4.20"),
        source_provider="test",
        retrieved_at=now,
    )


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
    open_interest: int | None = None,
    volume: int | None = None,
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
            open_interest=open_interest,
            volume=volume,
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

    def test_underlying_price_never_leaks_a_future_price_bar(self, db_session):
        """The whole point of ``as_of``-scoped snapshots: a price bar dated
        after the options snapshot was taken must never be used as "the"
        underlying price, even when it's the most recent one on record --
        that would be using information not yet available at snapshot time.
        """
        company = _seed_company(db_session)
        # Correct: on record before the options snapshot's date (2025-09-15).
        _seed_price_bar(db_session, company.ticker, date(2025, 9, 12), Decimal("114.50"))
        # Must be ignored: dated *after* the options snapshot -- using this
        # would be a lookahead-bias bug.
        _seed_price_bar(db_session, company.ticker, date(2025, 9, 20), Decimal("999.00"))
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
        expected_pct = (Decimal("8.40") / Decimal("114.50")).quantize(
            Decimal("0.000001"), rounding=ROUND_HALF_UP
        )
        assert row.implied_move_pct == expected_pct

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
        assert row.target_earnings_date == EARNINGS_DATE
        # Only one forward expiration on record -- no term structure or
        # ratios to compute without open_interest/volume data.
        assert row.next_term_expiration is None
        assert row.atm_iv_next is None
        assert row.term_structure_slope is None
        assert row.put_call_open_interest_ratio is None
        assert row.put_call_volume_ratio is None

        persisted = db_session.query(VolatilitySnapshot).filter_by(company_id=company.id).all()
        assert len(persisted) == 1

    def test_persists_term_structure_and_put_call_ratios_when_data_supports_them(self, db_session):
        company = _seed_company(db_session)
        next_exp = date(2025, 10, 17)
        _seed_price_bar(db_session, company.ticker, date(2025, 9, 12), Decimal("114.50"))
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("4.20"),
            ask=Decimal("4.40"),
            iv=Decimal("0.40"),
            open_interest=1000,
            volume=400,
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.PUT,
            bid=Decimal("4.00"),
            ask=Decimal("4.20"),
            iv=Decimal("0.42"),
            open_interest=1500,
            volume=600,
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=next_exp,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("6.00"),
            ask=Decimal("6.20"),
            iv=Decimal("0.50"),
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=next_exp,
            strike=Decimal("115"),
            option_type=OptionType.PUT,
            bid=Decimal("5.80"),
            ask=Decimal("6.00"),
            iv=Decimal("0.52"),
        )

        row = compute_and_persist_volatility_snapshot(db_session, company, EARNINGS_DATE)

        assert row is not None
        assert row.near_term_expiration == NEAR_EXP
        assert row.next_term_expiration == next_exp
        assert row.atm_iv_near == Decimal("0.41")
        assert row.atm_iv_next == Decimal("0.51")
        assert row.term_structure_slope == Decimal("0.10")
        assert row.put_call_open_interest_ratio == Decimal("1500") / Decimal("1000")
        assert row.put_call_volume_ratio == Decimal("600") / Decimal("400")

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


class TestGetImpliedVsRealizedMoves:
    def test_returns_empty_list_when_no_snapshots_exist(self, db_session):
        company = _seed_company(db_session)
        assert get_implied_vs_realized_moves(db_session, company.id) == []

    def test_excludes_snapshot_whose_earnings_date_has_no_reported_event(self, db_session):
        company = _seed_company(db_session)
        db_session.add(
            VolatilitySnapshot(
                company_id=company.id,
                snapshot_timestamp=SNAPSHOT_TS,
                method="atm_straddle",
                target_earnings_date=EARNINGS_DATE,
                implied_move_pct=Decimal("0.073"),
                computed_at=datetime.now(UTC),
            )
        )
        db_session.flush()

        assert get_implied_vs_realized_moves(db_session, company.id) == []

    def test_excludes_reported_event_with_no_next_day_move_recorded(self, db_session):
        company = _seed_company(db_session)
        event = EarningsEvent(
            company_id=company.id, fiscal_year=2025, fiscal_quarter=3, earnings_date=EARNINGS_DATE
        )
        db_session.add(event)
        db_session.flush()
        db_session.add(
            PriceReaction(
                earnings_event_id=event.id,
                next_day_move_pct=None,
                source_provider="test",
                retrieved_at=datetime.now(UTC),
            )
        )
        db_session.add(
            VolatilitySnapshot(
                company_id=company.id,
                snapshot_timestamp=SNAPSHOT_TS,
                method="atm_straddle",
                target_earnings_date=EARNINGS_DATE,
                implied_move_pct=Decimal("0.073"),
                computed_at=datetime.now(UTC),
            )
        )
        db_session.flush()

        assert get_implied_vs_realized_moves(db_session, company.id) == []

    def test_matches_snapshot_to_its_eventual_realized_move(self, db_session):
        company = _seed_company(db_session)
        event = EarningsEvent(
            company_id=company.id, fiscal_year=2025, fiscal_quarter=3, earnings_date=EARNINGS_DATE
        )
        db_session.add(event)
        db_session.flush()
        db_session.add(
            PriceReaction(
                earnings_event_id=event.id,
                next_day_move_pct=Decimal("-0.06"),
                source_provider="test",
                retrieved_at=datetime.now(UTC),
            )
        )
        db_session.add(
            VolatilitySnapshot(
                company_id=company.id,
                snapshot_timestamp=SNAPSHOT_TS,
                method="atm_straddle",
                target_earnings_date=EARNINGS_DATE,
                near_term_expiration=NEAR_EXP,
                implied_move_pct=Decimal("0.073"),
                computed_at=datetime.now(UTC),
            )
        )
        db_session.flush()

        results = get_implied_vs_realized_moves(db_session, company.id)

        assert len(results) == 1
        assert results[0].target_earnings_date == EARNINGS_DATE
        assert results[0].implied_move_pct == Decimal("0.073")
        assert results[0].realized_next_day_move_pct == Decimal("-0.06")

    def test_returns_every_forward_snapshot_for_the_same_earnings_date(self, db_session):
        company = _seed_company(db_session)
        event = EarningsEvent(
            company_id=company.id, fiscal_year=2025, fiscal_quarter=3, earnings_date=EARNINGS_DATE
        )
        db_session.add(event)
        db_session.flush()
        db_session.add(
            PriceReaction(
                earnings_event_id=event.id,
                next_day_move_pct=Decimal("-0.06"),
                source_provider="test",
                retrieved_at=datetime.now(UTC),
            )
        )
        earlier_ts = datetime(2025, 9, 8, 15, 0, tzinfo=UTC)
        db_session.add(
            VolatilitySnapshot(
                company_id=company.id,
                snapshot_timestamp=earlier_ts,
                method="atm_straddle",
                target_earnings_date=EARNINGS_DATE,
                implied_move_pct=Decimal("0.065"),
                computed_at=datetime.now(UTC),
            )
        )
        db_session.add(
            VolatilitySnapshot(
                company_id=company.id,
                snapshot_timestamp=SNAPSHOT_TS,
                method="atm_straddle",
                target_earnings_date=EARNINGS_DATE,
                implied_move_pct=Decimal("0.073"),
                computed_at=datetime.now(UTC),
            )
        )
        db_session.flush()

        results = get_implied_vs_realized_moves(db_session, company.id)

        assert len(results) == 2
        assert results[0].snapshot_timestamp == earlier_ts
        assert results[1].snapshot_timestamp == SNAPSHOT_TS


class TestCollectForwardOptionsSnapshot:
    def test_does_not_fetch_when_today_is_not_a_scheduled_collection_day(self, db_session):
        company = _seed_company(db_session)
        provider = _StubOptionsProvider([_stub_quote(company.ticker, Decimal("115"), "call")])
        off_schedule_as_of = datetime(2025, 9, 10, 15, 0, tzinfo=UTC)

        result = collect_forward_options_snapshot(
            db_session, provider, company, EARNINGS_DATE, off_schedule_as_of
        )

        assert result is None
        assert provider.call_count == 0
        assert db_session.query(OptionsSnapshot).count() == 0

    def test_fetches_and_persists_on_a_scheduled_collection_day(self, db_session):
        company = _seed_company(db_session)
        quotes = [
            _stub_quote(company.ticker, Decimal("115"), "call"),
            _stub_quote(company.ticker, Decimal("115"), "put"),
        ]
        provider = _StubOptionsProvider(quotes)
        t_minus_7 = datetime(2025, 9, 15, 15, 0, tzinfo=UTC)  # EARNINGS_DATE - 7 days

        result = collect_forward_options_snapshot(
            db_session, provider, company, EARNINGS_DATE, t_minus_7
        )

        assert result is not None
        assert len(result) == 2
        assert provider.call_count == 1
        persisted = db_session.query(OptionsSnapshot).filter_by(company_id=company.id).all()
        assert len(persisted) == 2
        assert persisted[0].snapshot_timestamp == t_minus_7

    def test_does_not_refetch_if_already_collected_today(self, db_session):
        company = _seed_company(db_session)
        provider = _StubOptionsProvider([_stub_quote(company.ticker, Decimal("115"), "call")])
        t_minus_1_morning = datetime(2025, 9, 21, 9, 0, tzinfo=UTC)  # EARNINGS_DATE - 1 day
        t_minus_1_afternoon = datetime(2025, 9, 21, 16, 0, tzinfo=UTC)

        first = collect_forward_options_snapshot(
            db_session, provider, company, EARNINGS_DATE, t_minus_1_morning
        )
        second = collect_forward_options_snapshot(
            db_session, provider, company, EARNINGS_DATE, t_minus_1_afternoon
        )

        assert first is not None
        assert len(first) == 1
        assert second is None
        assert provider.call_count == 1
        assert db_session.query(OptionsSnapshot).filter_by(company_id=company.id).count() == 1
