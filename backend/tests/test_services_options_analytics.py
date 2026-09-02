from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal

from analytics.market_session import EASTERN
from models.company import Company
from models.earnings_event import EarningsEvent
from models.enums import (
    DataState,
    MarketDataQuality,
    OptionsSnapshotAnchor,
    OptionsSnapshotPurpose,
    OptionType,
)
from models.options_snapshot import OptionsSnapshot
from models.price_bar import PriceBar
from models.price_reaction import PriceReaction
from models.volatility_snapshot import VolatilitySnapshot
from providers.base import OptionsDataProvider
from providers.types import OptionQuote
from services.options_analytics import (
    collect_forward_options_snapshot,
    collect_options_snapshot_now,
    compute_actionability,
    compute_and_persist_volatility_snapshot,
    compute_options_market_state,
    get_implied_vs_realized_moves,
    get_latest_close_price,
    get_latest_options_chain,
    get_latest_volatility_snapshot,
    select_pricing_snapshot,
)

SNAPSHOT_TS = datetime(2025, 9, 15, 15, 0, tzinfo=UTC)
EARNINGS_DATE = date(2025, 9, 22)
NEAR_EXP = date(2025, 9, 26)
FAR_EXP = date(2025, 9, 19)  # before earnings -- must never be selected


class _StubOptionsProvider(OptionsDataProvider):
    def __init__(self, quotes: list[OptionQuote]) -> None:
        self._quotes = quotes
        self.call_count = 0

    def get_option_chain(
        self, ticker, as_of, expiration=None, reference_date=None, earnings_anchored=True
    ):
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


class TestComputeAndPersistVolatilitySnapshotGeneralMode:
    """``earnings_date=None`` -- the general/current path added to unblock
    a ticker with no known upcoming earnings date (real bug: AMD,
    2026-08-18 -- see services/research_orchestration.py).
    """

    def test_uses_nearest_expiration_on_or_after_snapshot_date_and_labels_general(self, db_session):
        company = _seed_company(db_session)
        _seed_price_bar(db_session, company.ticker, date(2025, 9, 12), Decimal("114.50"))
        # Snapshot taken 2025-09-15 (SNAPSHOT_TS); an expiration exactly on
        # that date must be a valid candidate in general mode -- unlike
        # earnings-anchored mode's strictly-after rule.
        _seed_option_quote(
            db_session,
            company,
            expiration=date(2025, 9, 15),
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("4.20"),
            ask=Decimal("4.40"),
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=date(2025, 9, 15),
            strike=Decimal("115"),
            option_type=OptionType.PUT,
            bid=Decimal("4.00"),
            ask=Decimal("4.20"),
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("6.00"),
            ask=Decimal("6.20"),
        )

        row = compute_and_persist_volatility_snapshot(db_session, company, None)

        assert row is not None
        assert row.near_term_expiration == date(2025, 9, 15)
        assert row.target_earnings_date is None
        assert row.anchor == OptionsSnapshotAnchor.GENERAL_CURRENT

    def test_earnings_anchored_mode_still_labels_earnings_anchored(self, db_session):
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
        assert row.anchor == OptionsSnapshotAnchor.EARNINGS_ANCHORED
        assert row.target_earnings_date == EARNINGS_DATE


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
        assert db_session.query(OptionsSnapshot).filter_by(company_id=company.id).count() == 0

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


class TestCollectOptionsSnapshotNowAnchorLabeling:
    def test_earnings_date_known_stamps_earnings_anchored(self, db_session):
        company = _seed_company(db_session)
        provider = _StubOptionsProvider([_stub_quote(company.ticker, Decimal("115"), "call")])
        as_of = datetime(2025, 9, 15, 15, 0, tzinfo=UTC)

        result = collect_options_snapshot_now(db_session, provider, company, EARNINGS_DATE, as_of)

        assert result is not None
        assert len(result) == 1
        assert result[0].anchor == OptionsSnapshotAnchor.EARNINGS_ANCHORED

    def test_no_earnings_date_still_collects_and_stamps_general_current(self, db_session):
        """Regression test for the real bug this architecture replaces:
        collection must never be skipped just because no earnings date is
        known -- see services/research_orchestration.py::_prepare_options_chain.
        """
        company = _seed_company(db_session)
        provider = _StubOptionsProvider([_stub_quote(company.ticker, Decimal("115"), "call")])
        as_of = datetime(2025, 9, 15, 15, 0, tzinfo=UTC)

        result = collect_options_snapshot_now(db_session, provider, company, None, as_of)

        assert result is not None
        assert len(result) == 1
        assert result[0].anchor == OptionsSnapshotAnchor.GENERAL_CURRENT
        assert provider.call_count == 1


def test_get_latest_options_chain_preserves_market_data_quality_and_contract_id(db_session):
    # Real bug caught live building the Option Chain view (Phase 14): the
    # OptionsSnapshot -> OptionQuote conversion silently dropped
    # market_data_quality and external_contract_id, so every reader of
    # ingested chain data (implied move, strategy generation, and now the
    # chain view) lost IBKR's real live/delayed/frozen classification and
    # contract id even though both were correctly persisted.
    company = _seed_company(db_session)
    db_session.add(
        OptionsSnapshot(
            company_id=company.id,
            snapshot_timestamp=SNAPSHOT_TS,
            expiration_date=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("4.00"),
            ask=Decimal("4.20"),
            market_data_quality="frozen",
            external_contract_id="12345",
            source_provider="ibkr",
            retrieved_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    chain = get_latest_options_chain(db_session, company)

    assert len(chain) == 1
    assert chain[0].market_data_quality == "frozen"
    assert chain[0].external_contract_id == "12345"


def _quote_with(
    snapshot_timestamp: datetime,
    quality: str | None,
    source: str = "ibkr",
    bid: Decimal | None = None,
    ask: Decimal | None = None,
    implied_volatility: Decimal | None = None,
    delta: Decimal | None = None,
    anchor: str | None = None,
    expiration_date: date = NEAR_EXP,
):
    return OptionQuote(
        ticker="ZZSTATE",
        snapshot_timestamp=snapshot_timestamp,
        expiration_date=expiration_date,
        strike=Decimal("100"),
        option_type="call",
        bid=bid,
        ask=ask,
        implied_volatility=implied_volatility,
        delta=delta,
        market_data_quality=quality,
        anchor=anchor,
        source_provider=source,
        retrieved_at=snapshot_timestamp,
    )


class TestComputeOptionsMarketState:
    def test_empty_chain_is_not_collected(self):
        state = compute_options_market_state([], datetime.now(UTC), None)
        assert state.chain_exists is False
        assert state.contract_count == 0
        assert state.data_state == DataState.NOT_COLLECTED
        assert state.source is None
        assert state.snapshot_timestamp is None
        assert state.snapshot_age_minutes is None
        assert state.snapshot_age_label is None
        assert state.implied_move_available is False
        assert state.earnings_anchored is None
        assert "No real options-chain snapshot" in state.reason

    def test_reads_timestamp_source_and_quality_from_the_first_quote(self):
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        snapshot_ts = datetime(2026, 3, 18, 11, 45, tzinfo=UTC)
        chain = [_quote_with(snapshot_ts, "live", source="ibkr")]

        state = compute_options_market_state(chain, as_of, None)

        assert state.chain_exists is True
        assert state.contract_count == 1
        assert state.source == "ibkr"
        assert state.snapshot_timestamp == snapshot_ts
        assert state.snapshot_age_minutes == 15
        assert state.snapshot_age_label == "15m"
        assert state.market_data_quality == "live"

    def test_previous_calendar_day_snapshot_is_previous_session(self):
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        snapshot_ts = datetime(2026, 3, 17, 20, 0, tzinfo=UTC)
        chain = [_quote_with(snapshot_ts, "live")]

        state = compute_options_market_state(chain, as_of, None)

        assert state.data_state == DataState.PREVIOUS_SESSION
        assert "previous trading session" in state.reason

    def test_chain_with_no_priceable_quotes_reports_bid_ask_unavailable(self):
        # Real AVGO bug: a real chain with IV/Greeks but no bid/ask/last on
        # any contract must never be presented as "no options data".
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        chain = [
            _quote_with(
                as_of,
                "delayed",
                implied_volatility=Decimal("0.57"),
                delta=Decimal("0.5"),
                anchor="earnings_anchored",
            )
        ]

        state = compute_options_market_state(chain, as_of, None)

        assert state.chain_exists is True
        assert state.has_bid_ask is False
        assert state.has_iv is True
        assert state.has_greeks is True
        assert state.priceable_contract_count == 0
        assert state.implied_move_available is False
        assert state.earnings_anchored is True
        assert "no contract has a usable bid/ask/last price" in state.reason
        # bid_ask_contract_count requires BOTH bid and ask -- this quote has
        # neither -- but IV/Greeks were both supplied on the one contract.
        assert state.bid_ask_contract_count == 0
        assert state.iv_contract_count == 1
        assert state.greeks_contract_count == 1
        assert state.volume_coverage == 0.0
        assert state.oi_coverage == 0.0

    def test_chain_quality_summary_counts_and_coverage_are_per_contract(self):
        # A mixed chain: one contract with a full two-sided market, real
        # volume, and OI; one with only an IV/Greeks read (no bid/ask, no
        # volume, no OI) -- the counts must reflect exactly this split, not
        # collapse into the has_bid_ask/has_iv/has_greeks booleans.
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        priced = OptionQuote(
            ticker="ZZSTATE",
            snapshot_timestamp=as_of,
            expiration_date=NEAR_EXP,
            strike=Decimal("100"),
            option_type="call",
            bid=Decimal("1.90"),
            ask=Decimal("2.10"),
            volume=42,
            open_interest=100,
            implied_volatility=Decimal("0.5"),
            delta=Decimal("0.5"),
            market_data_quality="live",
            source_provider="ibkr",
            retrieved_at=as_of,
        )
        frozen = _quote_with(
            as_of, "frozen", implied_volatility=Decimal("0.6"), delta=Decimal("0.4")
        )

        state = compute_options_market_state([priced, frozen], as_of, None)

        assert state.contract_count == 2
        assert state.priceable_contract_count == 1
        assert state.bid_ask_contract_count == 1
        assert state.iv_contract_count == 2
        assert state.greeks_contract_count == 2
        assert state.volume_coverage == 0.5
        assert state.oi_coverage == 0.5

    def test_chain_with_bid_ask_but_no_volatility_snapshot_yet(self):
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        chain = [_quote_with(as_of, "live", bid=Decimal("1.90"), ask=Decimal("2.10"))]

        state = compute_options_market_state(chain, as_of, None)

        assert state.has_bid_ask is True
        assert state.priceable_contract_count == 1
        assert state.implied_move_available is False
        assert "no matching at-the-money call/put pair" in state.reason

    def test_implied_move_available_when_a_volatility_snapshot_exists(self, db_session):
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        chain = [_quote_with(as_of, "live", bid=Decimal("1.90"), ask=Decimal("2.10"))]
        company = _seed_company(db_session, ticker="ZZMKTSTATE")
        volatility = VolatilitySnapshot(
            company_id=company.id,
            snapshot_timestamp=as_of,
            method="atm_straddle",
            near_term_expiration=NEAR_EXP,
            implied_move_pct=Decimal("0.05"),
            implied_move_absolute=Decimal("5.00"),
            computed_at=as_of,
        )
        db_session.add(volatility)
        db_session.flush()

        state = compute_options_market_state(chain, as_of, volatility)

        assert state.implied_move_available is True
        assert state.expiration == NEAR_EXP
        assert "implied move was computed" in state.reason

    def test_general_current_anchor_is_reported_as_not_earnings_anchored(self):
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        chain = [_quote_with(as_of, "live", anchor="general_current")]

        state = compute_options_market_state(chain, as_of, None)

        assert state.earnings_anchored is False
        assert "not anchored to a confirmed earnings date" in state.reason


class TestGetLatestClosePrice:
    def test_none_when_no_price_bars_exist(self, db_session):
        assert get_latest_close_price(db_session, "ZZNOPRICE") is None

    def test_returns_the_most_recent_close_by_trade_date(self, db_session):
        company = _seed_company(db_session, ticker="ZZPRICE1")
        db_session.add_all(
            [
                PriceBar(
                    ticker="ZZPRICE1",
                    company_id=company.id,
                    trade_date=date(2026, 3, 16),
                    open=Decimal("10"),
                    high=Decimal("11"),
                    low=Decimal("9"),
                    close=Decimal("10.50"),
                    volume=100,
                    source_provider="test",
                    retrieved_at=datetime.now(UTC),
                ),
                PriceBar(
                    ticker="ZZPRICE1",
                    company_id=company.id,
                    trade_date=date(2026, 3, 17),
                    open=Decimal("11"),
                    high=Decimal("12"),
                    low=Decimal("10"),
                    close=Decimal("11.75"),
                    volume=150,
                    source_provider="test",
                    retrieved_at=datetime.now(UTC),
                ),
            ]
        )
        db_session.commit()

        assert get_latest_close_price(db_session, "ZZPRICE1") == Decimal("11.75")


def _seed_contract(
    db_session,
    company: Company,
    *,
    snapshot_timestamp: datetime,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
    last_price: Decimal | None = None,
    expiration: date = NEAR_EXP,
    strike: Decimal = Decimal("115"),
    option_type: OptionType = OptionType.CALL,
    quality: MarketDataQuality = MarketDataQuality.FROZEN,
    purpose: OptionsSnapshotPurpose = OptionsSnapshotPurpose.INTRADAY,
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
            last_price=last_price,
            market_data_quality=quality,
            purpose=purpose,
            source_provider="test",
            retrieved_at=snapshot_timestamp,
        )
    )
    db_session.flush()


class TestSelectPricingSnapshot:
    def test_no_snapshot_ever_collected_returns_none_tier(self, db_session):
        company = _seed_company(db_session, ticker="ZZSEL01")
        selection = select_pricing_snapshot(db_session, company)
        assert selection.tier == "none"
        assert selection.quotes == []
        assert selection.is_fallback is False

    def test_priceable_current_snapshot_is_used_directly(self, db_session):
        company = _seed_company(db_session, ticker="ZZSEL02")
        _seed_contract(
            db_session,
            company,
            snapshot_timestamp=SNAPSHOT_TS,
            bid=Decimal("4.00"),
            ask=Decimal("4.20"),
        )
        selection = select_pricing_snapshot(db_session, company)
        assert selection.tier == "current_priceable"
        assert selection.is_fallback is False
        assert len(selection.quotes) == 1

    def test_falls_back_to_most_recent_priceable_past_snapshot(self, db_session):
        company = _seed_company(db_session, ticker="ZZSEL03")
        older_priceable = SNAPSHOT_TS.replace(day=13)
        newer_unpriceable = SNAPSHOT_TS
        # Current (latest) snapshot: frozen, no priceable contract.
        _seed_contract(db_session, company, snapshot_timestamp=newer_unpriceable)
        # An older snapshot that IS priceable.
        _seed_contract(
            db_session,
            company,
            snapshot_timestamp=older_priceable,
            bid=Decimal("3.80"),
            ask=Decimal("4.00"),
        )
        selection = select_pricing_snapshot(db_session, company)
        assert selection.tier == "previous_priceable"
        assert selection.is_fallback is True
        assert selection.snapshot_timestamp == older_priceable

    def test_prefers_the_most_recent_priceable_snapshot_among_several_past_ones(self, db_session):
        company = _seed_company(db_session, ticker="ZZSEL04")
        newest = SNAPSHOT_TS
        middle = SNAPSHOT_TS.replace(day=14)
        oldest = SNAPSHOT_TS.replace(day=12)
        _seed_contract(db_session, company, snapshot_timestamp=newest)  # unpriceable
        _seed_contract(
            db_session,
            company,
            snapshot_timestamp=middle,
            bid=Decimal("3.90"),
            ask=Decimal("4.10"),
        )
        _seed_contract(
            db_session,
            company,
            snapshot_timestamp=oldest,
            bid=Decimal("3.50"),
            ask=Decimal("3.70"),
        )
        selection = select_pricing_snapshot(db_session, company)
        assert selection.tier == "previous_priceable"
        assert selection.snapshot_timestamp == middle

    def test_contracts_only_when_nothing_anywhere_is_priceable(self, db_session):
        company = _seed_company(db_session, ticker="ZZSEL05")
        older = SNAPSHOT_TS.replace(day=13)
        _seed_contract(db_session, company, snapshot_timestamp=older)
        _seed_contract(db_session, company, snapshot_timestamp=SNAPSHOT_TS)
        selection = select_pricing_snapshot(db_session, company)
        assert selection.tier == "contracts_only"
        assert selection.is_fallback is False
        assert selection.snapshot_timestamp == SNAPSHOT_TS
        assert len(selection.quotes) == 1

    def test_purpose_is_carried_through_from_the_selected_snapshot(self, db_session):
        company = _seed_company(db_session, ticker="ZZSEL06")
        older_close = SNAPSHOT_TS.replace(day=13)
        _seed_contract(db_session, company, snapshot_timestamp=SNAPSHOT_TS)  # unpriceable
        _seed_contract(
            db_session,
            company,
            snapshot_timestamp=older_close,
            bid=Decimal("3.80"),
            ask=Decimal("4.00"),
            purpose=OptionsSnapshotPurpose.CLOSE,
        )
        selection = select_pricing_snapshot(db_session, company)
        assert selection.purpose == "close"

    def test_priceable_via_last_price_alone_counts(self, db_session):
        """bid/ask are both None but last_price is real -- matches the
        existing has_bid_ask definition (any of bid/ask/last_price)."""
        company = _seed_company(db_session, ticker="ZZSEL07")
        _seed_contract(
            db_session, company, snapshot_timestamp=SNAPSHOT_TS, last_price=Decimal("4.10")
        )
        selection = select_pricing_snapshot(db_session, company)
        assert selection.tier == "current_priceable"


class TestComputeActionability:
    def test_none_tier_is_unavailable(self):
        assert compute_actionability("none", None, datetime.now(UTC)) == "unavailable"

    def test_contracts_only_tier_stays_contracts_only(self):
        as_of = datetime.now(UTC)
        assert compute_actionability("contracts_only", as_of, as_of) == "contracts_only"

    def test_current_priceable_from_todays_still_open_session_is_actionable_current(self):
        # Wednesday 11:00 ET, snapshot taken earlier the same still-open
        # session (market opened 09:30 ET) -- genuinely live/current.
        as_of = datetime(2026, 3, 18, 11, 0, tzinfo=EASTERN)
        snapshot_ts = datetime(2026, 3, 18, 9, 35, tzinfo=EASTERN)
        assert (
            compute_actionability("current_priceable", snapshot_ts, as_of) == "actionable_current"
        )

    def test_current_priceable_that_is_actually_yesterdays_close_is_not_actionable_current(self):
        # Regression for a real live bug (MU, 2026-08-19): the latest
        # *collected* snapshot being priceable does not mean it's actually
        # from today -- nothing may have re-collected since yesterday's
        # close. That must be labeled/gated exactly like the explicit
        # previous_priceable fallback path, never silently as "current".
        as_of = datetime(2026, 3, 18, 11, 0, tzinfo=EASTERN)
        snapshot_ts = datetime(2026, 3, 17, 15, 58, tzinfo=EASTERN)
        assert (
            compute_actionability("current_priceable", snapshot_ts, as_of)
            == "actionable_previous_session"
        )

    def test_current_priceable_two_sessions_old_is_stale(self):
        as_of = datetime(2026, 3, 18, 11, 0, tzinfo=EASTERN)
        snapshot_ts = datetime(2026, 3, 16, 15, 58, tzinfo=EASTERN)  # Monday -- 2 sessions back
        assert (
            compute_actionability("current_priceable", snapshot_ts, as_of) == "stale_research_only"
        )

    def test_current_priceable_captured_near_todays_close_becomes_previous_session_after_close(
        self,
    ):
        # A near-close capture (e.g. 15:58 ET) is genuinely live while the
        # session is still open, but once viewed after today's own 16:00
        # ET close it must read as "previous session", not "current" -- the
        # market is shut, nothing is live anymore, matching
        # previous_trading_session_date's own "today counts as previous
        # once today's close has passed" rule.
        snapshot_ts = datetime(2026, 3, 18, 15, 58, tzinfo=EASTERN)
        still_open = datetime(2026, 3, 18, 15, 59, tzinfo=EASTERN)
        assert (
            compute_actionability("current_priceable", snapshot_ts, still_open)
            == "actionable_current"
        )
        after_close = datetime(2026, 3, 18, 17, 0, tzinfo=EASTERN)
        assert (
            compute_actionability("current_priceable", snapshot_ts, after_close)
            == "actionable_previous_session"
        )

    def test_previous_priceable_missing_timestamp_is_stale(self):
        # Defensive-only path -- select_pricing_snapshot never actually
        # returns previous_priceable with snapshot_timestamp=None.
        assert (
            compute_actionability("previous_priceable", None, datetime.now(UTC))
            == "stale_research_only"
        )

    def test_previous_priceable_matching_the_prior_session_is_actionable(self):
        # Wednesday 11:00 ET -- previous session is Tuesday 2026-03-17.
        as_of = datetime(2026, 3, 18, 11, 0, tzinfo=EASTERN)
        snapshot_ts = datetime(2026, 3, 17, 15, 58, tzinfo=EASTERN)
        assert (
            compute_actionability("previous_priceable", snapshot_ts, as_of)
            == "actionable_previous_session"
        )

    def test_previous_priceable_two_sessions_old_is_stale(self):
        as_of = datetime(2026, 3, 18, 11, 0, tzinfo=EASTERN)
        snapshot_ts = datetime(2026, 3, 16, 15, 58, tzinfo=EASTERN)  # Monday -- 2 sessions back
        assert (
            compute_actionability("previous_priceable", snapshot_ts, as_of) == "stale_research_only"
        )


class TestComputeOptionsMarketStateFallbackLabeling:
    def test_fallback_snapshot_is_labeled_previous_session_not_previous_close_by_default(
        self, db_session
    ):
        company = _seed_company(db_session, ticker="ZZSEL08")
        # SNAPSHOT_TS is Monday 2025-09-15 11:00 ET (the current,
        # unpriceable snapshot); `older` is the immediately preceding
        # real trading session -- Friday 2025-09-12 -- and `as_of` is
        # later that same Monday but still before its own regular close,
        # so previous_trading_session_date(as_of) resolves to Friday too.
        older = SNAPSHOT_TS.replace(day=12)
        as_of = SNAPSHOT_TS.replace(hour=16)
        _seed_contract(db_session, company, snapshot_timestamp=SNAPSHOT_TS)
        _seed_contract(
            db_session,
            company,
            snapshot_timestamp=older,
            bid=Decimal("3.80"),
            ask=Decimal("4.00"),
        )
        selection = select_pricing_snapshot(db_session, company)
        state = compute_options_market_state(selection.quotes, as_of, None, selection)
        assert state.is_fallback_snapshot is True
        assert state.snapshot_tier == "previous_priceable"
        assert state.actionability == "actionable_previous_session"
        assert state.data_state == DataState.PREVIOUS_SESSION
        assert "Previous-session snapshot" in state.reason
        assert "Previous close" not in state.reason

    def test_fallback_snapshot_captured_as_close_is_labeled_previous_close(self, db_session):
        company = _seed_company(db_session, ticker="ZZSEL09")
        older = SNAPSHOT_TS.replace(day=12)
        as_of = SNAPSHOT_TS.replace(hour=16)
        _seed_contract(db_session, company, snapshot_timestamp=SNAPSHOT_TS)
        _seed_contract(
            db_session,
            company,
            snapshot_timestamp=older,
            bid=Decimal("3.80"),
            ask=Decimal("4.00"),
            purpose=OptionsSnapshotPurpose.CLOSE,
        )
        selection = select_pricing_snapshot(db_session, company)
        state = compute_options_market_state(selection.quotes, as_of, None, selection)
        assert state.actionability == "actionable_previous_session"
        assert "Previous close" in state.reason

    def test_fallback_snapshot_two_sessions_old_is_stale_research_only(self, db_session):
        """Phase 14.11 Part 4: a fallback snapshot from two or more real
        trading sessions ago is a HARD GATE -- STALE, never labeled as an
        actionable previous-session snapshot no matter how complete its
        own pricing looked at capture time."""
        company = _seed_company(db_session, ticker="ZZSEL08STALE")
        # `older` here is Thursday 2025-09-11 -- two sessions before the
        # Monday `as_of` used above (Thursday, then Friday, then Monday).
        older = SNAPSHOT_TS.replace(day=11)
        as_of = SNAPSHOT_TS.replace(hour=16)
        _seed_contract(db_session, company, snapshot_timestamp=SNAPSHOT_TS)
        _seed_contract(
            db_session,
            company,
            snapshot_timestamp=older,
            bid=Decimal("3.80"),
            ask=Decimal("4.00"),
        )
        selection = select_pricing_snapshot(db_session, company)
        state = compute_options_market_state(selection.quotes, as_of, None, selection)
        assert state.actionability == "stale_research_only"
        assert "STALE" in state.reason
        assert "RESEARCH ONLY" in state.reason

    def test_no_selection_argument_never_claims_a_fallback(self, db_session):
        """Callers still using the plain current-only chain (no
        PricingSnapshotSelection) must never have is_fallback_snapshot
        reported True -- that would be a fabricated provenance claim."""
        company = _seed_company(db_session, ticker="ZZSEL10")
        _seed_contract(
            db_session,
            company,
            snapshot_timestamp=SNAPSHOT_TS,
            bid=Decimal("4.00"),
            ask=Decimal("4.20"),
        )
        raw_chain = get_latest_options_chain(db_session, company)
        state = compute_options_market_state(raw_chain, datetime.now(UTC), None)
        assert state.is_fallback_snapshot is False
        assert state.snapshot_tier == "current_priceable"


class TestComputeAndPersistVolatilitySnapshotFallback:
    def test_computes_implied_move_from_fallback_snapshot_when_current_chain_unpriceable(
        self, db_session
    ):
        """The real AAPL-shaped bug this policy fixes: the current chain
        is frozen with zero priceable contracts, but an older snapshot for
        the same company has a real, priceable ATM call/put pair -- the
        implied move must be computed from that older snapshot rather than
        giving up."""
        company = _seed_company(db_session, ticker="ZZSEL11")
        _seed_price_bar(db_session, company.ticker, date(2025, 9, 12), Decimal("115.00"))

        # Current snapshot: frozen, no bid/ask/last on either leg.
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=None,
            ask=None,
            snapshot_timestamp=SNAPSHOT_TS,
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.PUT,
            bid=None,
            ask=None,
            snapshot_timestamp=SNAPSHOT_TS,
        )
        # An older snapshot with real, priceable quotes at the same ATM strike.
        older = SNAPSHOT_TS.replace(day=12)
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=Decimal("4.20"),
            ask=Decimal("4.40"),
            snapshot_timestamp=older,
        )
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.PUT,
            bid=Decimal("4.00"),
            ask=Decimal("4.20"),
            snapshot_timestamp=older,
        )

        result = compute_and_persist_volatility_snapshot(db_session, company, EARNINGS_DATE)
        assert result is not None
        assert result.snapshot_timestamp == older
        assert result.implied_move_pct is not None

    def test_returns_none_when_no_snapshot_anywhere_is_priceable(self, db_session):
        company = _seed_company(db_session, ticker="ZZSEL12")
        _seed_price_bar(db_session, company.ticker, date(2025, 9, 12), Decimal("115.00"))
        _seed_option_quote(
            db_session,
            company,
            expiration=NEAR_EXP,
            strike=Decimal("115"),
            option_type=OptionType.CALL,
            bid=None,
            ask=None,
            snapshot_timestamp=SNAPSHOT_TS,
        )
        assert compute_and_persist_volatility_snapshot(db_session, company, EARNINGS_DATE) is None
