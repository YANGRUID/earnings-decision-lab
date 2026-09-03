"""services/us_listing.py -- SEC exchange listings as the US-listing source."""

from services.us_listing import UsListingCheck, sec_ticker_form


class _FakeEdgar:
    def __init__(self, listings: dict[str, str], fail: bool = False):
        self.listings = listings
        self.fail = fail
        self.calls = 0

    def list_exchange_listings(self) -> dict[str, str]:
        self.calls += 1
        if self.fail:
            raise ConnectionError("sec.gov unreachable")
        return dict(self.listings)


def test_sec_ticker_form_uses_dashes_for_class_shares():
    assert sec_ticker_form("BF.A") == "BF-A"
    assert sec_ticker_form(" lulu ") == "LULU"


def test_exchange_lookup_matches_calendar_and_sec_ticker_forms_and_caches():
    edgar = _FakeEdgar({"LULU": "Nasdaq", "BF-A": "NYSE", "NIO": "NYSE"})
    check = UsListingCheck(edgar)  # type: ignore[arg-type]
    assert check.exchange_for("LULU") == "Nasdaq"
    assert check.exchange_for("BF.A") == "NYSE"
    assert check.exchange_for("SHOP") is None
    assert edgar.calls == 1  # one fetch, then the cached map


def test_cache_expires_after_its_ttl():
    edgar = _FakeEdgar({"LULU": "Nasdaq"})
    check = UsListingCheck(edgar, ttl_seconds=0)  # type: ignore[arg-type]
    check.exchange_for("LULU")
    check.exchange_for("LULU")
    assert edgar.calls == 2


def test_fetch_failure_raises_instead_of_answering_not_listed():
    check = UsListingCheck(_FakeEdgar({}, fail=True))  # type: ignore[arg-type]
    try:
        check.exchange_for("LULU")
    except ConnectionError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a failed fetch must not look like 'not listed'")
