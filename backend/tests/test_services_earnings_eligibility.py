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
    def __init__(self, expirations: list[date] | None = None, raise_error: bool = False) -> None:
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
    assert result.retryable is False  # a real, permanent business-rule rejection


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
    assert result.retryable is False


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
    # A real, empty chain (the call succeeded, there's genuinely nothing
    # tradable) is not the same as a transient provider-call failure --
    # never worth retrying on its own.
    assert result.retryable is False


def test_options_chain_lookup_failure_skipped_not_raised():
    """Post-live correction (2026-08-25): real Aug 25 evidence -- WSM's
    preparation-time options-chain probe hit a genuine IBKR rate limit
    (a transient failure of the provider CALL itself, not a real,
    data-driven ineligibility verdict) and was recorded exactly like a
    permanent hard filter, even though WSM's own later, independent
    execution-time check succeeded minutes afterward and produced a real
    DecisionSnapshot. retryable=True is what lets callers (services/
    earnings_research_preparation.py) represent this honestly instead."""
    event = _event()
    result = check_eligibility(event, _FakeOptionsProvider(raise_error=True))

    assert result.eligible is False
    assert result.reason is not None and "options chain lookup failed" in result.reason
    assert result.retryable is True


# --- v4.0.1: "US listed" is a listing fact, not a domicile fact ------------


def test_foreign_domiciled_company_listed_on_a_us_exchange_is_eligible():
    """Lululemon: Canadian company, Nasdaq listing -- the case that exposed the
    country-only rule as wrong."""
    event = _event(symbol="LULU", country="CA")
    result = check_eligibility(
        event,
        _FakeOptionsProvider(),
        us_listing=lambda symbol: "Nasdaq" if symbol == "LULU" else None,
    )
    assert result.eligible is True


def test_foreign_company_without_a_us_listing_is_rejected_for_listing_not_domicile():
    event = _event(symbol="SHOP", country="CA")
    result = check_eligibility(event, _FakeOptionsProvider(), us_listing=lambda _symbol: None)
    assert result.eligible is False
    assert result.reason is not None and "no SEC-registered US exchange listing" in result.reason
    assert result.retryable is False


def test_listing_lookup_failure_is_unverified_and_retryable_never_not_listed():
    def _boom(_symbol: str) -> str | None:
        raise TimeoutError("sec.gov timed out")

    event = _event(symbol="LULU", country="CA")
    result = check_eligibility(event, _FakeOptionsProvider(), us_listing=_boom)
    assert result.eligible is False
    assert result.retryable is True
    assert result.reason is not None and "unavailable" in result.reason
    assert "not US listed" not in result.reason


def test_us_domiciled_company_never_needs_the_lookup():
    calls: list[str] = []

    def _lookup(symbol: str) -> str | None:
        calls.append(symbol)
        return None

    assert check_eligibility(
        _event(country="US"), _FakeOptionsProvider(), us_listing=_lookup
    ).eligible
    assert calls == []


def test_without_a_lookup_the_country_rule_still_applies():
    result = check_eligibility(_event(country="CA"), _FakeOptionsProvider())
    assert result.eligible is False and "not US listed" in (result.reason or "")
