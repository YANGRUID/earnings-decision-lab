from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.options.expiration_selection import (
    build_expiration_candidate,
    build_expiration_reasons,
    select_best_expiration,
)
from providers.types import OptionQuote

RETRIEVED = datetime(2026, 8, 19, 16, 0, tzinfo=UTC)
REFERENCE = date(2026, 8, 19)
EARNINGS = date(2026, 8, 26)
UNDERLYING = Decimal("100")


def _quote(
    expiration: date,
    strike: Decimal,
    option_type: str = "call",
    bid: Decimal | None = Decimal("1.00"),
    ask: Decimal | None = Decimal("1.10"),
    last_price: Decimal | None = None,
    iv: Decimal | None = Decimal("0.30"),
    open_interest: int | None = 100,
    volume: int | None = 50,
    market_data_quality: str | None = "delayed",
) -> OptionQuote:
    return OptionQuote(
        ticker="TEST",
        snapshot_timestamp=RETRIEVED,
        expiration_date=expiration,
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        last_price=last_price,
        implied_volatility=iv,
        open_interest=open_interest,
        volume=volume,
        market_data_quality=market_data_quality,
        source_provider="test",
        retrieved_at=RETRIEVED,
    )


def _good_chain(expiration: date) -> list[OptionQuote]:
    return [
        _quote(expiration, Decimal("95")),
        _quote(expiration, Decimal("100")),
        _quote(expiration, Decimal("105")),
        _quote(expiration, Decimal("95"), option_type="put"),
        _quote(expiration, Decimal("100"), option_type="put"),
        _quote(expiration, Decimal("105"), option_type="put"),
    ]


class TestBuildExpirationCandidate:
    def test_empty_quotes_is_untradeable(self):
        candidate = build_expiration_candidate(
            expiration=date(2026, 9, 4),
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=[],
            underlying_price=UNDERLYING,
        )
        assert candidate.contract_count == 0
        assert candidate.quality == "untradeable"
        assert candidate.score.total == 0

    def test_good_chain_scores_full_liquidity_and_quote_coverage(self):
        exp = date(2026, 9, 4)  # 9 days after earnings -- inside event-fit full window
        candidate = build_expiration_candidate(
            expiration=exp,
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=_good_chain(exp),
            underlying_price=UNDERLYING,
        )
        assert candidate.quality == "good"
        assert candidate.contract_count == 6
        assert candidate.priceable_contract_count == 6
        assert candidate.quote_coverage == Decimal(1)
        assert candidate.bid_ask_coverage == Decimal(1)
        assert candidate.score.liquidity == 20  # full WEIGHT_LIQUIDITY
        assert candidate.score.quote_coverage == 15  # full WEIGHT_QUOTE_COVERAGE
        assert candidate.atm_iv == Decimal("0.30")
        assert not candidate.excluded_pre_earnings

    def test_on_or_before_earnings_is_excluded(self):
        exp = date(2026, 8, 21)  # before EARNINGS (8/26)
        candidate = build_expiration_candidate(
            expiration=exp,
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=_good_chain(exp),
            underlying_price=UNDERLYING,
        )
        assert candidate.excluded_pre_earnings is True
        assert candidate.score.event_fit == 0

    def test_no_earnings_date_never_excludes_and_scores_full_event_fit(self):
        exp = date(2026, 8, 21)
        candidate = build_expiration_candidate(
            expiration=exp,
            reference_date=REFERENCE,
            earnings_date=None,
            quotes=_good_chain(exp),
            underlying_price=UNDERLYING,
        )
        assert candidate.excluded_pre_earnings is False
        assert candidate.score.event_fit == 25  # full WEIGHT_EVENT_FIT
        assert candidate.days_after_earnings is None

    def test_zero_priceable_contracts_is_untradeable_even_with_real_contracts(self):
        exp = date(2026, 9, 4)
        contracts_only = [
            _quote(exp, Decimal("100"), bid=None, ask=None, last_price=None)
            for _ in range(3)
        ]
        candidate = build_expiration_candidate(
            expiration=exp,
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=contracts_only,
            underlying_price=UNDERLYING,
        )
        assert candidate.contract_count == 3
        assert candidate.priceable_contract_count == 0
        assert candidate.quality == "untradeable"

    def test_wide_spread_scores_lower_bid_ask_quality_than_tight_spread(self):
        exp = date(2026, 9, 4)
        tight = build_expiration_candidate(
            expiration=exp,
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=[_quote(exp, UNDERLYING, bid=Decimal("1.00"), ask=Decimal("1.02"))],
            underlying_price=UNDERLYING,
        )
        wide = build_expiration_candidate(
            expiration=exp,
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=[_quote(exp, UNDERLYING, bid=Decimal("0.50"), ask=Decimal("1.50"))],
            underlying_price=UNDERLYING,
        )
        assert tight.score.bid_ask_quality > wide.score.bid_ask_quality

    def test_dte_sweet_spot_scores_higher_than_very_short_or_very_long(self):
        sweet = build_expiration_candidate(
            expiration=date(2026, 8, 30),  # 11 DTE from REFERENCE
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=_good_chain(date(2026, 8, 30)),
            underlying_price=UNDERLYING,
        )
        very_short = build_expiration_candidate(
            expiration=date(2026, 8, 20),  # 1 DTE
            reference_date=REFERENCE,
            earnings_date=None,
            quotes=_good_chain(date(2026, 8, 20)),
            underlying_price=UNDERLYING,
        )
        very_long = build_expiration_candidate(
            expiration=date(2026, 11, 20),  # ~93 DTE
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=_good_chain(date(2026, 11, 20)),
            underlying_price=UNDERLYING,
        )
        assert sweet.score.dte_suitability > very_short.score.dte_suitability
        assert sweet.score.dte_suitability > very_long.score.dte_suitability

    def test_data_quality_reflects_worst_market_data_quality_present(self):
        exp = date(2026, 9, 4)
        live_quotes = [_quote(exp, UNDERLYING, market_data_quality="live")]
        frozen_quotes = [_quote(exp, UNDERLYING, market_data_quality="frozen")]
        live = build_expiration_candidate(
            expiration=exp,
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=live_quotes,
            underlying_price=UNDERLYING,
        )
        frozen = build_expiration_candidate(
            expiration=exp,
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=frozen_quotes,
            underlying_price=UNDERLYING,
        )
        assert live.score.data_quality > frozen.score.data_quality


class TestSelectBestExpiration:
    def test_empty_list_returns_none(self):
        assert select_best_expiration([]) is None

    def test_never_selects_pre_earnings_candidate_even_if_it_scores_highest(self):
        pre_earnings = build_expiration_candidate(
            expiration=date(2026, 8, 21),
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=_good_chain(date(2026, 8, 21)),
            underlying_price=UNDERLYING,
        )
        post_earnings = build_expiration_candidate(
            expiration=date(2026, 9, 4),
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=[
                _quote(date(2026, 9, 4), UNDERLYING, bid=None, ask=None, last_price=Decimal("1"))
            ],
            underlying_price=UNDERLYING,
        )
        selected = select_best_expiration([pre_earnings, post_earnings])
        assert selected is not None
        assert selected.expiration == date(2026, 9, 4)

    def test_never_selects_untradeable_candidate(self):
        untradeable = build_expiration_candidate(
            expiration=date(2026, 9, 4),
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=[_quote(date(2026, 9, 4), UNDERLYING, bid=None, ask=None, last_price=None)],
            underlying_price=UNDERLYING,
        )
        assert select_best_expiration([untradeable]) is None

    def test_picks_highest_total_score(self):
        good = build_expiration_candidate(
            expiration=date(2026, 9, 4),
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=_good_chain(date(2026, 9, 4)),
            underlying_price=UNDERLYING,
        )
        poor = build_expiration_candidate(
            expiration=date(2026, 9, 11),
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=[
                _quote(date(2026, 9, 11), UNDERLYING, bid=None, ask=None, last_price=Decimal("1"))
            ],
            underlying_price=UNDERLYING,
        )
        selected = select_best_expiration([good, poor])
        assert selected is not None
        assert selected.expiration == date(2026, 9, 4)


class TestBuildExpirationReasons:
    def test_reasons_are_grounded_in_real_numbers(self):
        exp = date(2026, 9, 4)
        selected = build_expiration_candidate(
            expiration=exp,
            reference_date=REFERENCE,
            earnings_date=EARNINGS,
            quotes=_good_chain(exp),
            underlying_price=UNDERLYING,
        )
        reasons = build_expiration_reasons(selected, [])
        assert any("100%" in r for r in reasons)
        assert any(str(selected.days_after_earnings) in r for r in reasons)

    def test_no_reasons_crash_with_empty_alternatives(self):
        exp = date(2026, 9, 4)
        selected = build_expiration_candidate(
            expiration=exp,
            reference_date=REFERENCE,
            earnings_date=None,
            quotes=[],
            underlying_price=None,
        )
        reasons = build_expiration_reasons(selected, [])
        assert isinstance(reasons, list)
