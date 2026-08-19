from datetime import UTC, date, datetime
from decimal import Decimal

from models.company import Company
from models.price_bar import PriceBar
from providers.types import OptionQuote
from services.expiration_engine import resolve_auto_expiration, resolve_manual_expiration

AS_OF = datetime(2026, 8, 19, 16, 0, tzinfo=UTC)
REFERENCE = date(2026, 8, 19)
EARNINGS = date(2026, 8, 26)


def _seed_company(db_session, ticker: str = "ZZEXP1") -> Company:
    company = Company(ticker=ticker, name="ZZ Expiration Test Co", cik="0009999950")
    db_session.add(company)
    db_session.flush()
    return company


def _seed_price(db_session, ticker: str, price: Decimal = Decimal("100")) -> None:
    db_session.add(
        PriceBar(
            ticker=ticker,
            trade_date=REFERENCE,
            open=price,
            high=price,
            low=price,
            close=price,
            volume=1_000_000,
            source_provider="test",
            retrieved_at=AS_OF,
        )
    )
    db_session.flush()


def _quote(expiration: date, strike: Decimal) -> OptionQuote:
    return OptionQuote(
        ticker="TEST",
        snapshot_timestamp=AS_OF,
        expiration_date=expiration,
        strike=strike,
        option_type="call",
        bid=Decimal("1.00"),
        ask=Decimal("1.10"),
        implied_volatility=Decimal("0.30"),
        open_interest=100,
        volume=50,
        market_data_quality="delayed",
        source_provider="test",
        retrieved_at=AS_OF,
    )


class _FakeProvider:
    """A minimal OptionsDataProvider double: real listed expirations and
    real per-expiration chains, both hardcoded rather than fetched -- this
    tests the orchestration in services/expiration_engine.py, not IBKR's
    HTTP flow (already covered by tests/test_providers_ibkr_options.py)."""

    def __init__(self, expirations: list[date], chains: dict[date, list[OptionQuote]]) -> None:
        self._expirations = expirations
        self._chains = chains
        self.chain_fetch_count = 0

    def get_option_chain(self, ticker, as_of, expiration=None, **kwargs) -> list[OptionQuote]:
        self.chain_fetch_count += 1
        return self._chains.get(expiration, [])

    def list_available_expirations(self, ticker, after, max_candidates=5) -> list[date]:
        return [e for e in self._expirations if e > after][:max_candidates]


def _good_chain(expiration: date) -> list[OptionQuote]:
    return [_quote(expiration, Decimal("100")), _quote(expiration, Decimal("105"))]


def _unpriceable_quote(expiration: date, last_price: Decimal | None = None) -> OptionQuote:
    return OptionQuote(
        ticker="TEST",
        snapshot_timestamp=AS_OF,
        expiration_date=expiration,
        strike=Decimal("100"),
        option_type="call",
        bid=None,
        ask=None,
        last_price=last_price,
        source_provider="test",
        retrieved_at=AS_OF,
    )


class TestResolveAutoExpiration:
    def test_picks_best_among_real_discovered_candidates(self, db_session):
        company = _seed_company(db_session)
        _seed_price(db_session, company.ticker)
        weekly = date(2026, 9, 4)
        monthly = date(2026, 10, 16)
        provider = _FakeProvider(
            expirations=[weekly, monthly],
            chains={weekly: _good_chain(weekly), monthly: _good_chain(monthly)},
        )

        result = resolve_auto_expiration(db_session, company, provider, AS_OF, EARNINGS)

        assert result.mode == "auto"
        assert result.selected is not None
        assert result.selected.expiration == weekly  # closer to the DTE sweet spot
        assert monthly in {c.expiration for c in result.alternatives}
        assert result.reasons  # real, non-empty explanation

    def test_no_discovered_expirations_returns_none_with_warning(self, db_session):
        company = _seed_company(db_session, "ZZEXP2")
        _seed_price(db_session, company.ticker)
        provider = _FakeProvider(expirations=[], chains={})

        result = resolve_auto_expiration(db_session, company, provider, AS_OF, EARNINGS)

        assert result.selected is None
        assert result.warning is not None

    def test_all_candidates_untradeable_returns_none_but_keeps_alternatives(self, db_session):
        company = _seed_company(db_session, "ZZEXP3")
        _seed_price(db_session, company.ticker)
        exp = date(2026, 9, 4)
        provider = _FakeProvider(expirations=[exp], chains={exp: [_unpriceable_quote(exp)]})

        result = resolve_auto_expiration(db_session, company, provider, AS_OF, EARNINGS)

        assert result.selected is None
        assert len(result.alternatives) == 1
        assert result.warning is not None


class TestResolveManualExpiration:
    def test_uses_only_the_chosen_expiration(self, db_session):
        company = _seed_company(db_session, "ZZEXP4")
        _seed_price(db_session, company.ticker)
        chosen = date(2026, 9, 18)
        other = date(2026, 9, 4)
        provider = _FakeProvider(
            expirations=[other, chosen],
            chains={chosen: _good_chain(chosen), other: _good_chain(other)},
        )

        result = resolve_manual_expiration(
            db_session, company, provider, AS_OF, EARNINGS, chosen, compare_against_auto=False
        )

        assert result.mode == "manual"
        assert result.selected is not None
        assert result.selected.expiration == chosen
        assert provider.chain_fetch_count == 1  # never fetched `other`

    def test_flags_materially_worse_manual_choice_without_blocking_it(self, db_session):
        company = _seed_company(db_session, "ZZEXP5")
        _seed_price(db_session, company.ticker)
        good = date(2026, 9, 4)
        poor_manual = date(2026, 12, 18)  # far DTE, thin/poor chain
        thin_quote = _unpriceable_quote(poor_manual, last_price=Decimal("1"))
        provider = _FakeProvider(
            expirations=[good, poor_manual],
            chains={good: _good_chain(good), poor_manual: [thin_quote]},
        )

        result = resolve_manual_expiration(
            db_session, company, provider, AS_OF, EARNINGS, poor_manual, compare_against_auto=True
        )

        assert result.selected is not None
        assert result.selected.expiration == poor_manual  # never silently substituted
        assert result.warning is not None
        assert "materially worse" in result.warning

    def test_pre_earnings_manual_choice_is_flagged(self, db_session):
        company = _seed_company(db_session, "ZZEXP6")
        _seed_price(db_session, company.ticker)
        pre_earnings = date(2026, 8, 21)
        provider = _FakeProvider(
            expirations=[pre_earnings], chains={pre_earnings: _good_chain(pre_earnings)}
        )

        result = resolve_manual_expiration(
            db_session,
            company,
            provider,
            AS_OF,
            EARNINGS,
            pre_earnings,
            compare_against_auto=False,
        )

        assert result.selected is not None
        assert result.warning is not None
        assert "before the earnings date" in result.warning
