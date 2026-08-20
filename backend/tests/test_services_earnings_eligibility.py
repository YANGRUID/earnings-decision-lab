"""Phase 4.2 -- unit tests for services/earnings_eligibility.py. Uses an
in-memory fake OptionsDataProvider, never a live provider call."""

from datetime import date, datetime
from decimal import Decimal

from models.earnings_calendar_event import EarningsCalendarEvent
from models.enums import EarningsTiming
from providers.base import OptionsDataProvider
from providers.types import OptionQuote
from services.earnings_eligibility import check_eligibility


class _FakeOptionsProvider(OptionsDataProvider):
    def __init__(
        self, expirations: list[date] | None = None, raise_error: bool = False
    ) -> None:
        self._expirations = expirations if expirations is not None else [date(2026, 12, 18)]
        self._raise_error = raise_error

    def get_option_chain(
        self,
        ticker: str,
        as_of: datetime,
        expiration: date | None = None,
        reference_date: date | None = None,
        earnings_anchored: bool = True,
    ) -> list[OptionQuote]:
        return []

    def list_available_expirations(
        self, ticker: str, after: date, max_candidates: int = 5
    ) -> list[date]:
        if self._raise_error:
            raise RuntimeError("options provider unavailable")
        return self._expirations


def _event(**overrides: object) -> EarningsCalendarEvent:
    defaults: dict[str, object] = dict(
        symbol="TESTNVDA",
        company_name="Test NVIDIA Corp",
        earnings_date=date(2026, 11, 25),
        earnings_time=EarningsTiming.AMC,
        market_cap=Decimal("3200000000000"),
        country="US",
    )
    defaults.update(overrides)
    return EarningsCalendarEvent(**defaults)  # type: ignore[arg-type]


def test_market_cap_below_10b_skipped():
    event = _event(market_cap=Decimal("5000000000"))  # $5B
    result = check_eligibility(event, _FakeOptionsProvider())

    assert result.eligible is False
    assert result.reason is not None and "market cap" in result.reason


def test_market_cap_unknown_skipped():
    event = _event(market_cap=None)
    result = check_eligibility(event, _FakeOptionsProvider())

    assert result.eligible is False
    assert result.reason == "market cap unknown"


def test_valid_company_accepted():
    event = _event()
    result = check_eligibility(event, _FakeOptionsProvider())

    assert result.eligible is True
    assert result.reason is None
    assert result.symbol == "TESTNVDA"


def test_non_us_listed_skipped():
    event = _event(country="CA")
    result = check_eligibility(event, _FakeOptionsProvider())

    assert result.eligible is False
    assert result.reason is not None and "not US listed" in result.reason


def test_no_options_provider_configured_skipped():
    event = _event()
    result = check_eligibility(event, None)

    assert result.eligible is False
    assert result.reason == "no options provider configured"


def test_no_tradable_option_chain_skipped():
    event = _event()
    result = check_eligibility(event, _FakeOptionsProvider(expirations=[]))

    assert result.eligible is False
    assert result.reason == "no tradable option chain"


def test_options_chain_lookup_failure_skipped_not_raised():
    event = _event()
    result = check_eligibility(event, _FakeOptionsProvider(raise_error=True))

    assert result.eligible is False
    assert result.reason is not None and "options chain lookup failed" in result.reason
