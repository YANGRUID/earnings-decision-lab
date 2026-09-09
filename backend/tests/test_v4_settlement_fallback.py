"""The end-of-day settlement fallback hierarchy (authorized 2026-09-04).

Pins the order, the boundaries, and -- most of all -- what must NEVER
happen: no model price, no prior-day close, no midpoint, no last-trade
substitution, and no writing a living option down to zero just because its
book was empty at the settlement instant.
"""

from decimal import Decimal

import pytest

from services.v4_settlement_fallback import (
    PRICING_EXECUTABLE_ASK,
    PRICING_EXECUTABLE_BID,
    PRICING_EXPIRATION_INTRINSIC_AT_CLOSE,
    PRICING_MARKET_CLOSE_FALLBACK,
    UNRESOLVED_NO_CLOSING_MARK,
    UNRESOLVED_NO_UNDERLYING_CLOSE,
    UNRESOLVED_SESSION_STILL_OPEN,
    expiration_intrinsic,
    required_exit_side,
    resolve_leg_exit_price,
)


def _resolve(**overrides):
    kwargs = {
        "action": "buy",
        "right": "call",
        "strike": Decimal("127"),
        "executable_price": None,
        "session_close": None,
        "underlying_close": None,
        "expires_on_settlement_date": True,
    }
    kwargs.update(overrides)
    return resolve_leg_exit_price(**kwargs)


class TestRequiredSideIsUnchanged:
    def test_a_long_leg_closes_on_the_bid(self):
        assert required_exit_side("buy") == "bid"

    def test_a_short_leg_closes_on_the_ask(self):
        assert required_exit_side("sell") == "ask"


class TestExecutableSideAlwaysWins:
    def test_a_real_bid_is_used_and_labelled_executable(self):
        out = _resolve(executable_price=Decimal("0.75"), session_close=Decimal("1.10"))
        assert out.price == Decimal("0.75")
        assert out.pricing_source == PRICING_EXECUTABLE_BID
        assert out.is_executable

    def test_a_real_ask_closes_a_short_leg(self):
        out = _resolve(action="sell", executable_price=Decimal("0.20"))
        assert out.price == Decimal("0.20")
        assert out.pricing_source == PRICING_EXECUTABLE_ASK

    def test_a_closing_mark_never_overrides_a_real_executable_quote(self):
        out = _resolve(
            executable_price=Decimal("0.01"),
            session_close=Decimal("9.99"),
            underlying_close=Decimal("500"),
        )
        assert out.price == Decimal("0.01")
        assert out.pricing_source == PRICING_EXECUTABLE_BID


class TestClosingMarkIsTheFirstFallback:
    def test_the_option_session_close_is_used_when_no_executable_side_exists(self):
        out = _resolve(session_close=Decimal("0.04"), underlying_close=Decimal("120"))
        assert out.price == Decimal("0.04")
        assert out.pricing_source == PRICING_MARKET_CLOSE_FALLBACK
        assert not out.is_executable, "a closing mark is not an executable trade"

    def test_the_closing_mark_wins_over_intrinsic_even_on_expiry_day(self):
        out = _resolve(
            session_close=Decimal("0.04"),
            underlying_close=Decimal("500"),
            expires_on_settlement_date=True,
        )
        assert out.pricing_source == PRICING_MARKET_CLOSE_FALLBACK

    def test_the_closing_mark_is_recorded_in_provenance(self):
        out = _resolve(session_close=Decimal("0.04"))
        assert out.provenance["option_session_close"] == "0.04"


class TestExpirationIntrinsicIsLastAndExpiryOnly:
    def test_an_expiring_call_uses_intrinsic_against_the_underlying_close(self):
        out = _resolve(right="call", strike=Decimal("127"), underlying_close=Decimal("131.50"))
        assert out.price == Decimal("4.50")
        assert out.pricing_source == PRICING_EXPIRATION_INTRINSIC_AT_CLOSE

    def test_an_expiring_out_of_the_money_call_is_worth_zero(self):
        out = _resolve(right="call", strike=Decimal("127"), underlying_close=Decimal("120"))
        assert out.price == Decimal("0")
        assert out.pricing_source == PRICING_EXPIRATION_INTRINSIC_AT_CLOSE

    def test_an_expiring_put_uses_the_other_side(self):
        out = _resolve(right="put", strike=Decimal("36"), underlying_close=Decimal("33.25"))
        assert out.price == Decimal("2.75")

    def test_a_non_expiring_option_is_never_written_down_to_zero(self):
        """Rule 4: an empty book on a living option means unquoted, not
        worthless."""
        out = _resolve(
            expires_on_settlement_date=False,
            underlying_close=Decimal("120"),
            book_empty=True,
        )
        assert out.price is None
        assert out.pricing_source is None
        assert out.unresolved_reason == UNRESOLVED_NO_CLOSING_MARK

    def test_an_expiring_leg_without_an_underlying_close_stays_unresolved(self):
        out = _resolve(underlying_close=None)
        assert out.price is None
        assert out.unresolved_reason == UNRESOLVED_NO_UNDERLYING_CLOSE

    @pytest.mark.parametrize(
        "right,strike,close,expected",
        [
            ("call", "100", "105", "5"),
            ("call", "100", "95", "0"),
            ("put", "100", "95", "5"),
            ("put", "100", "105", "0"),
        ],
    )
    def test_intrinsic_is_never_negative(self, right, strike, close, expected):
        assert expiration_intrinsic(right, Decimal(strike), Decimal(close)) == Decimal(expected)


class TestNoForbiddenSubstitution:
    def test_a_last_trade_alone_resolves_nothing(self):
        """last is not an input to this hierarchy at all -- passing only a
        last price must leave the leg unresolved."""
        out = _resolve(expires_on_settlement_date=False)
        assert out.price is None

    def test_provenance_always_records_what_was_relied_on(self):
        out = _resolve(underlying_close=Decimal("131.50"))
        assert out.provenance["underlying_close"] == "131.50"
        assert out.provenance["expires_on_settlement_date"] is True
        assert out.required_side == "bid"


class TestAClosingMarkThatDoesNotExistYet:
    """Live incident (2026-09-09, 15:45 ET).

    An emergency recovery run fifteen minutes before the close priced three
    empty-book legs from the last RTH 30-minute trade bar and labelled the
    result MARKET_CLOSE_FALLBACK. The data was genuinely same-session and the
    timestamp was honest, but the label claimed a finality the session had not
    reached: the released hierarchy says "final verifiable same-session closing
    mark", and at 15:45 no such thing exists.

    Executable settlement is unaffected -- that is the whole point. Only the
    closing-mark tier waits for an actual close.
    """

    def test_the_close_tier_is_refused_while_the_session_is_open(self):
        out = resolve_leg_exit_price(
            action="buy",
            right="put",
            strike=Decimal("15"),
            executable_price=None,
            session_close=Decimal("0.05"),
            underlying_close=None,
            expires_on_settlement_date=False,
            book_empty=True,
            session_close_is_final=False,
        )
        assert out.resolved is False
        assert out.pricing_source is None
        assert out.unresolved_reason == UNRESOLVED_SESSION_STILL_OPEN
        # The mark is recorded as seen but explicitly not used.
        assert out.provenance["intraday_mark_available"] == "0.05"

    def test_the_same_leg_settles_once_the_session_has_closed(self):
        out = resolve_leg_exit_price(
            action="buy",
            right="put",
            strike=Decimal("15"),
            executable_price=None,
            session_close=Decimal("0.05"),
            underlying_close=None,
            expires_on_settlement_date=False,
            book_empty=True,
            session_close_is_final=True,
        )
        assert out.resolved is True
        assert out.pricing_source == PRICING_MARKET_CLOSE_FALLBACK
        assert out.price == Decimal("0.05")

    def test_an_executable_price_still_settles_mid_session(self):
        """The guard must never delay a real executable settlement -- that is
        the tier the incident was trying to rescue in the first place."""
        out = resolve_leg_exit_price(
            action="buy",
            right="call",
            strike=Decimal("20"),
            executable_price=Decimal("0.20"),
            session_close=Decimal("0.25"),
            underlying_close=None,
            expires_on_settlement_date=False,
            session_close_is_final=False,
        )
        assert out.resolved is True
        assert out.pricing_source == PRICING_EXECUTABLE_BID
        assert out.price == Decimal("0.20")

    def test_the_default_stays_final_so_the_scheduled_post_close_job_is_unchanged(self):
        out = resolve_leg_exit_price(
            action="buy",
            right="put",
            strike=Decimal("15"),
            executable_price=None,
            session_close=Decimal("0.05"),
            underlying_close=None,
            expires_on_settlement_date=False,
            book_empty=True,
        )
        assert out.pricing_source == PRICING_MARKET_CLOSE_FALLBACK
