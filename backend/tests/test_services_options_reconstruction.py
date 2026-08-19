from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.market_session import EASTERN
from core.config import get_settings
from models.company import Company
from models.enums import OptionsSnapshotPurpose, OptionType
from models.options_snapshot import OptionsSnapshot
from providers.ibkr_client import IBKRNotAuthenticatedError
from providers.types import OptionQuote
from services.options_reconstruction import (
    CLOSE_WINDOW_END,
    ChainQualitySummary,
    ReconstructionResult,
    classify_chain_quality,
    determine_target_close_window,
    find_best_persisted_close_snapshot,
    resolve_best_actionable_option_market,
)

NEAR_EXP = date(2026, 3, 20)


def _seed_company(db_session, ticker: str = "ZZRECON1") -> Company:
    company = Company(ticker=ticker, name="ZZ Reconstruction Test Co", cik="0009999940")
    db_session.add(company)
    db_session.flush()
    return company


def _seed_snapshot(
    db_session,
    company: Company,
    *,
    snapshot_timestamp: datetime,
    purpose: OptionsSnapshotPurpose = OptionsSnapshotPurpose.INTRADAY,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
    last_price: Decimal | None = None,
    strike: Decimal = Decimal("100"),
) -> None:
    db_session.add(
        OptionsSnapshot(
            company_id=company.id,
            snapshot_timestamp=snapshot_timestamp,
            expiration_date=NEAR_EXP,
            strike=strike,
            option_type=OptionType.CALL,
            bid=bid,
            ask=ask,
            last_price=last_price,
            purpose=purpose,
            source_provider="test",
            retrieved_at=snapshot_timestamp,
        )
    )
    db_session.flush()


def _quote(bid=None, ask=None, last_price=None, iv=None, delta=None, oi=None, volume=None):
    now = datetime.now(UTC)
    return OptionQuote(
        ticker="ZZ",
        snapshot_timestamp=now,
        expiration_date=NEAR_EXP,
        strike=Decimal("100"),
        option_type="call",
        bid=bid,
        ask=ask,
        last_price=last_price,
        implied_volatility=iv,
        delta=delta,
        open_interest=oi,
        volume=volume,
        source_provider="test",
        retrieved_at=now,
    )


class TestDetermineTargetCloseWindow:
    def test_pre_market_resolves_to_previous_weekday(self):
        # Wednesday 2026-03-18, 08:00 ET (pre-market) -- target is Tuesday.
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)  # 08:00 ET
        target, start_utc, end_utc = determine_target_close_window(as_of)
        assert target == date(2026, 3, 17)
        assert start_utc.astimezone(EASTERN).time().hour == 15
        assert start_utc.astimezone(EASTERN).time().minute == 55
        assert end_utc.astimezone(EASTERN).time() == CLOSE_WINDOW_END

    def test_after_close_resolves_to_today(self):
        # Wednesday 2026-03-18, 17:00 ET (after today's own close).
        as_of = datetime(2026, 3, 18, 21, 0, tzinfo=UTC)  # 17:00 ET
        target, _start, _end = determine_target_close_window(as_of)
        assert target == date(2026, 3, 18)

    def test_weekend_resolves_to_friday(self):
        # Saturday 2026-03-21.
        as_of = datetime(2026, 3, 21, 15, 0, tzinfo=UTC)
        target, _start, _end = determine_target_close_window(as_of)
        assert target == date(2026, 3, 20)  # Friday


class TestClassifyChainQuality:
    def test_empty_chain_is_untradeable(self):
        result = classify_chain_quality([])
        assert result.quality == "untradeable"
        assert result.contract_count == 0

    def test_zero_priceable_is_untradeable(self):
        quotes = [_quote(iv=Decimal("0.5"), delta=Decimal("0.5")) for _ in range(5)]
        result = classify_chain_quality(quotes)
        assert result.quality == "untradeable"
        assert result.priceable_contract_count == 0

    def test_all_priceable_is_good(self):
        quotes = [_quote(last_price=Decimal("3.00")) for _ in range(10)]
        result = classify_chain_quality(quotes)
        assert result.quality == "good"
        assert result.priceable_contract_count == 10
        assert result.last_coverage == 1.0

    def test_half_priceable_is_acceptable(self):
        quotes = [_quote(last_price=Decimal("3.00")) for _ in range(5)] + [
            _quote() for _ in range(5)
        ]
        result = classify_chain_quality(quotes)
        assert result.quality == "acceptable"

    def test_below_40_percent_priceable_is_poor(self):
        quotes = [_quote(last_price=Decimal("3.00")) for _ in range(2)] + [
            _quote() for _ in range(8)
        ]
        result = classify_chain_quality(quotes)
        assert result.quality == "poor"

    def test_median_spread_computed_from_real_bid_ask(self):
        quotes = [
            _quote(bid=Decimal("1.90"), ask=Decimal("2.10")),  # spread ~10%
            _quote(bid=Decimal("1.00"), ask=Decimal("1.20")),  # spread ~18%
        ]
        result = classify_chain_quality(quotes)
        assert result.median_spread_pct is not None
        assert result.median_spread_pct > 0

    def test_no_bid_ask_pairs_leaves_median_spread_none(self):
        quotes = [_quote(last_price=Decimal("3.00"))]
        result = classify_chain_quality(quotes)
        assert result.median_spread_pct is None


class TestFindBestPersistedCloseSnapshot:
    def test_returns_none_when_nothing_on_that_date(self, db_session):
        company = _seed_company(db_session)
        result = find_best_persisted_close_snapshot(db_session, company, date(2026, 3, 17))
        assert result is None

    def test_prefers_close_purpose_over_intraday(self, db_session):
        company = _seed_company(db_session, "ZZRECON2")
        intraday_ts = datetime(2026, 3, 17, 17, 0, tzinfo=UTC)  # 13:00 ET
        close_ts = datetime(2026, 3, 17, 19, 58, tzinfo=UTC)  # 15:58 ET
        _seed_snapshot(
            db_session,
            company,
            snapshot_timestamp=intraday_ts,
            purpose=OptionsSnapshotPurpose.INTRADAY,
            bid=Decimal("1.90"),
            ask=Decimal("2.10"),
        )
        _seed_snapshot(
            db_session,
            company,
            snapshot_timestamp=close_ts,
            purpose=OptionsSnapshotPurpose.CLOSE,
            bid=Decimal("2.00"),
            ask=Decimal("2.20"),
        )
        result = find_best_persisted_close_snapshot(db_session, company, date(2026, 3, 17))
        assert result is not None
        assert result.purpose == "close"
        assert result.snapshot_timestamp == close_ts

    def test_a_1558_snapshot_beats_a_1300_snapshot_of_the_same_purpose(self, db_session):
        # Real, explicit example from the mandate: a 15:58 snapshot must
        # beat a 13:00 snapshot even when neither is a deliberate "close"
        # capture -- both intraday, ranked by distance from 16:00 ET.
        company = _seed_company(db_session, "ZZRECON3")
        early_ts = datetime(2026, 3, 17, 17, 0, tzinfo=UTC)  # 13:00 ET
        late_ts = datetime(2026, 3, 17, 19, 58, tzinfo=UTC)  # 15:58 ET
        _seed_snapshot(
            db_session,
            company,
            snapshot_timestamp=early_ts,
            purpose=OptionsSnapshotPurpose.INTRADAY,
            bid=Decimal("1.90"),
            ask=Decimal("2.10"),
        )
        _seed_snapshot(
            db_session,
            company,
            snapshot_timestamp=late_ts,
            purpose=OptionsSnapshotPurpose.INTRADAY,
            bid=Decimal("2.00"),
            ask=Decimal("2.20"),
        )
        result = find_best_persisted_close_snapshot(db_session, company, date(2026, 3, 17))
        assert result is not None
        assert result.snapshot_timestamp == late_ts

    def test_ignores_rows_from_other_dates(self, db_session):
        company = _seed_company(db_session, "ZZRECON4")
        other_day_ts = datetime(2026, 3, 16, 19, 58, tzinfo=UTC)
        _seed_snapshot(
            db_session,
            company,
            snapshot_timestamp=other_day_ts,
            bid=Decimal("2.00"),
            ask=Decimal("2.20"),
        )
        result = find_best_persisted_close_snapshot(db_session, company, date(2026, 3, 17))
        assert result is None

    def test_untradeable_candidate_is_still_returned_for_caller_to_judge(self, db_session):
        # find_best_persisted_close_snapshot itself doesn't filter by
        # quality -- resolve_best_actionable_option_market decides whether
        # "good"/"acceptable" is good enough; this function just ranks.
        company = _seed_company(db_session, "ZZRECON5")
        ts = datetime(2026, 3, 17, 19, 58, tzinfo=UTC)
        _seed_snapshot(db_session, company, snapshot_timestamp=ts)  # no bid/ask/last at all
        result = find_best_persisted_close_snapshot(db_session, company, date(2026, 3, 17))
        assert result is not None
        assert result.quality.quality == "untradeable"


class TestResolveBestActionableOptionMarket:
    def test_market_open_never_attempts_reconstruction(self, db_session):
        company = _seed_company(db_session, "ZZRECON6")
        # Wednesday 2026-03-18, 11:00 ET -- regular session.
        as_of = datetime(2026, 3, 18, 15, 0, tzinfo=UTC)
        resolution = resolve_best_actionable_option_market(
            db_session, company, as_of, earnings_date=None
        )
        assert resolution.reconstruction_attempted is False
        assert resolution.target_session_date is None

    def test_market_closed_uses_a_good_persisted_close_snapshot_without_reconstruction(
        self, db_session
    ):
        company = _seed_company(db_session, "ZZRECON7")
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)  # Wed 08:00 ET (pre-market)
        close_ts = datetime(2026, 3, 17, 19, 58, tzinfo=UTC)  # Tue 15:58 ET
        for strike, val in ((Decimal("95"), "1.90"), (Decimal("100"), "2.90")):
            _seed_snapshot(
                db_session,
                company,
                snapshot_timestamp=close_ts,
                purpose=OptionsSnapshotPurpose.CLOSE,
                bid=Decimal(val),
                ask=Decimal(val) + Decimal("0.20"),
                strike=strike,
            )
        resolution = resolve_best_actionable_option_market(
            db_session, company, as_of, earnings_date=None
        )
        assert resolution.reconstruction_attempted is False
        assert resolution.selection.tier == "previous_priceable"
        assert resolution.selection.purpose == "close"
        assert len(resolution.selection.quotes) == 2

    def test_market_closed_with_non_ibkr_provider_defers_without_attempting(
        self, db_session, monkeypatch
    ):
        company = _seed_company(db_session, "ZZRECON8")
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        settings = get_settings()
        monkeypatch.setattr(settings, "options_provider", "alpha_vantage")
        resolution = resolve_best_actionable_option_market(
            db_session, company, as_of, earnings_date=None, settings=settings
        )
        assert resolution.reconstruction_attempted is False

    def test_market_closed_ibkr_configured_but_unauthenticated_defers_gracefully(
        self, db_session, monkeypatch
    ):
        company = _seed_company(db_session, "ZZRECON9")
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        settings = get_settings()
        monkeypatch.setattr(settings, "options_provider", "ibkr")

        def _raise_not_authenticated(self):
            raise IBKRNotAuthenticatedError("not authenticated")

        monkeypatch.setattr(
            "services.options_reconstruction.IBKRClient.ensure_authenticated",
            _raise_not_authenticated,
        )
        resolution = resolve_best_actionable_option_market(
            db_session, company, as_of, earnings_date=None, settings=settings
        )
        assert resolution.reconstruction_attempted is False
        assert resolution.selection.tier in ("contracts_only", "none")

    def test_successful_reconstruction_is_persisted_and_used(self, db_session, monkeypatch):
        company = _seed_company(db_session, "ZZRECON10")
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        settings = get_settings()
        monkeypatch.setattr(settings, "options_provider", "ibkr")
        monkeypatch.setattr(
            "services.options_reconstruction.IBKRClient.ensure_authenticated",
            lambda self: None,
        )

        target_ts = datetime(2026, 3, 17, 19, 58, tzinfo=UTC)
        fake_quotes = [
            OptionQuote(
                ticker=company.ticker,
                snapshot_timestamp=target_ts,
                expiration_date=NEAR_EXP,
                strike=Decimal("100"),
                option_type="call",
                last_price=Decimal("3.00"),
                source_provider="ibkr",
                retrieved_at=datetime.now(UTC),
                underlying_price=Decimal("101.50"),
                underlying_timestamp=target_ts,
            )
        ]
        fake_result = ReconstructionResult(
            succeeded=True,
            reason="Reconstructed 1/1 contract(s) from IBKR historical data.",
            quotes=fake_quotes,
            quality=ChainQualitySummary(
                contract_count=1,
                priceable_contract_count=1,
                bid_ask_coverage=0.0,
                last_coverage=1.0,
                iv_coverage=0.0,
                greeks_coverage=0.0,
                oi_coverage=0.0,
                volume_coverage=0.0,
                median_spread_pct=None,
                quality="good",
            ),
            underlying_price=Decimal("101.50"),
            underlying_timestamp=target_ts,
            expiration=NEAR_EXP,
            snapshot_timestamp=target_ts,
        )
        monkeypatch.setattr(
            "services.options_reconstruction.reconstruct_close_snapshot",
            lambda *args, **kwargs: fake_result,
        )

        resolution = resolve_best_actionable_option_market(
            db_session, company, as_of, earnings_date=None, settings=settings
        )
        assert resolution.reconstruction_attempted is True
        assert resolution.reconstruction_result is not None
        assert resolution.reconstruction_result.succeeded is True
        assert resolution.selection.tier == "previous_priceable"
        assert resolution.selection.purpose == "reconstructed_close"
        assert len(resolution.selection.quotes) == 1

        persisted = (
            db_session.query(OptionsSnapshot)
            .filter(OptionsSnapshot.company_id == company.id)
            .all()
        )
        assert len(persisted) == 1
        assert persisted[0].purpose == OptionsSnapshotPurpose.RECONSTRUCTED_CLOSE
        assert persisted[0].reconstruction_source == "ibkr_historical"
        assert persisted[0].pricing_source == "historical_last"
        assert persisted[0].underlying_price == Decimal("101.50")

    def test_failed_reconstruction_defers_to_select_pricing_snapshot(self, db_session, monkeypatch):
        company = _seed_company(db_session, "ZZRECON11")
        as_of = datetime(2026, 3, 18, 12, 0, tzinfo=UTC)
        settings = get_settings()
        monkeypatch.setattr(settings, "options_provider", "ibkr")
        monkeypatch.setattr(
            "services.options_reconstruction.IBKRClient.ensure_authenticated",
            lambda self: None,
        )
        failed_result = ReconstructionResult(
            succeeded=False,
            reason="IBKR historical reconstruction found no usable contract pricing.",
            quotes=[],
            quality=None,
            underlying_price=None,
            underlying_timestamp=None,
            expiration=None,
            snapshot_timestamp=None,
        )
        monkeypatch.setattr(
            "services.options_reconstruction.reconstruct_close_snapshot",
            lambda *args, **kwargs: failed_result,
        )

        resolution = resolve_best_actionable_option_market(
            db_session, company, as_of, earnings_date=None, settings=settings
        )
        assert resolution.reconstruction_attempted is True
        assert resolution.reconstruction_result is not None
        assert resolution.reconstruction_result.succeeded is False
        # Nothing was persisted for a failed reconstruction.
        persisted = (
            db_session.query(OptionsSnapshot)
            .filter(OptionsSnapshot.company_id == company.id)
            .all()
        )
        assert persisted == []
