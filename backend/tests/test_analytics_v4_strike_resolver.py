"""V4.3 target-price -> listed-strike resolver tests (2026-09-02).
Verifies the resolver never invents a strike, honors/refuses
constraints honestly, and applies the documented deterministic
tie-break order (Section 6)."""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_strike_resolver import (
    StrikeConstraint,
    next_strike_beyond,
    resolve_target_to_strike,
)
from providers.types import OptionQuote

EXP = date(2026, 9, 18)
NOW = datetime(2026, 8, 17, tzinfo=UTC)


def _quote(
    strike: Decimal,
    option_type: str,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
    volume: int | None = None,
    open_interest: int | None = None,
    external_contract_id: str | None = None,
) -> OptionQuote:
    return OptionQuote(
        ticker="ZZ",
        snapshot_timestamp=NOW,
        expiration_date=EXP,
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        volume=volume,
        open_interest=open_interest,
        external_contract_id=external_contract_id,
        source_provider="test",
        retrieved_at=NOW,
    )


CALL_STRIKES = [Decimal(s) for s in (90, 95, 100, 105, 110)]


def _calls() -> list[OptionQuote]:
    return [_quote(s, "call", s * Decimal("0.02"), s * Decimal("0.02") + 1) for s in CALL_STRIKES]


class TestBasicResolution:
    def test_nearest_picks_closest_real_strike(self):
        r = resolve_target_to_strike(Decimal("103"), "call", _calls())
        assert r.resolvable is True
        assert r.selected_strike == Decimal("105")
        assert r.distance_dollars == Decimal("2")
        assert r.chain_size == 5
        assert r.hit_chain_edge is False

    def test_nearest_exact_match(self):
        r = resolve_target_to_strike(Decimal("100"), "call", _calls())
        assert r.selected_strike == Decimal("100")
        assert r.distance_dollars == Decimal("0")

    def test_nearest_above_constraint(self):
        r = resolve_target_to_strike(
            Decimal("101"), "call", _calls(), StrikeConstraint.NEAREST_ABOVE
        )
        assert r.selected_strike == Decimal("105")

    def test_nearest_below_constraint(self):
        r = resolve_target_to_strike(
            Decimal("101"), "call", _calls(), StrikeConstraint.NEAREST_BELOW
        )
        assert r.selected_strike == Decimal("100")

    def test_constraint_unsatisfiable_returns_unresolvable_not_reinterpreted(self):
        r = resolve_target_to_strike(
            Decimal("200"), "call", _calls(), StrikeConstraint.NEAREST_ABOVE
        )
        assert r.resolvable is False
        assert r.selected_strike is None
        assert "nearest_above" in r.reason

    def test_no_strikes_at_all_returns_unresolvable(self):
        r = resolve_target_to_strike(Decimal("100"), "put", _calls())  # only calls exist
        assert r.resolvable is False
        assert r.chain_size == 0
        assert "no listed put strikes" in r.reason

    def test_target_beyond_available_chain_flags_hit_chain_edge(self):
        r = resolve_target_to_strike(Decimal("500"), "call", _calls())
        assert r.resolvable is True
        assert r.selected_strike == Decimal("110")
        assert r.hit_chain_edge is True

    def test_target_within_chain_does_not_flag_hit_chain_edge(self):
        r = resolve_target_to_strike(Decimal("107"), "call", _calls())
        assert r.hit_chain_edge is False

    def test_strike_index_reported(self):
        r = resolve_target_to_strike(Decimal("100"), "call", _calls())
        assert r.strike_index == 2  # 90,95,100,105,110 -> index 2

    def test_external_contract_id_passed_through(self):
        quotes = [
            _quote(
                Decimal("100"), "call", Decimal("1"), Decimal("2"), external_contract_id="abc123"
            )
        ]
        r = resolve_target_to_strike(Decimal("100"), "call", quotes)
        assert r.external_contract_id == "abc123"


class TestQuoteQuality:
    def test_two_sided_quote(self):
        quotes = [_quote(Decimal("100"), "call", Decimal("1"), Decimal("2"))]
        r = resolve_target_to_strike(Decimal("100"), "call", quotes)
        assert r.quote_quality == "two_sided"

    def test_unquoted_when_no_bid_ask_or_last(self):
        quotes = [_quote(Decimal("100"), "call")]
        r = resolve_target_to_strike(Decimal("100"), "call", quotes)
        assert r.quote_quality == "unquoted"


class TestTieBreaking:
    """Two strikes exactly equidistant from the target -- the
    deterministic cascade (Section 6) must decide, never an arbitrary
    dict/set iteration order."""

    def test_prefers_two_sided_over_one_sided(self):
        quotes = [
            _quote(Decimal("95"), "call", None, None, volume=None),  # unquoted (no last either)
            _quote(Decimal("105"), "call", Decimal("5"), Decimal("6")),  # two-sided
        ]
        r = resolve_target_to_strike(Decimal("100"), "call", quotes)
        assert r.selected_strike == Decimal("105")

    def test_prefers_narrower_spread_when_quality_tied(self):
        quotes = [
            _quote(Decimal("95"), "call", Decimal("5"), Decimal("6")),  # 20% spread
            _quote(Decimal("105"), "call", Decimal("5"), Decimal("5.25")),  # 5% spread
        ]
        r = resolve_target_to_strike(Decimal("100"), "call", quotes)
        assert r.selected_strike == Decimal("105")

    def test_prefers_higher_liquidity_when_quality_and_spread_tied(self):
        quotes = [
            _quote(
                Decimal("95"), "call", Decimal("5"), Decimal("5.10"), volume=10, open_interest=50
            ),
            _quote(
                Decimal("105"),
                "call",
                Decimal("5"),
                Decimal("5.10"),
                volume=500,
                open_interest=2000,
            ),
        ]
        r = resolve_target_to_strike(Decimal("100"), "call", quotes)
        assert r.selected_strike == Decimal("105")

    def test_final_fallback_is_lower_strike_when_everything_else_tied(self):
        quotes = [
            _quote(
                Decimal("95"), "call", Decimal("5"), Decimal("5.10"), volume=10, open_interest=50
            ),
            _quote(
                Decimal("105"), "call", Decimal("5"), Decimal("5.10"), volume=10, open_interest=50
            ),
        ]
        r = resolve_target_to_strike(Decimal("100"), "call", quotes)
        assert r.selected_strike == Decimal("95")

    def test_tie_break_is_deterministic_across_repeated_calls(self):
        quotes = [
            _quote(
                Decimal("95"), "call", Decimal("5"), Decimal("5.10"), volume=10, open_interest=50
            ),
            _quote(
                Decimal("105"), "call", Decimal("5"), Decimal("5.10"), volume=10, open_interest=50
            ),
        ]
        results = {
            resolve_target_to_strike(Decimal("100"), "call", quotes).selected_strike
            for _ in range(10)
        }
        assert results == {Decimal("95")}


class TestNextStrikeBeyond:
    def test_up(self):
        assert next_strike_beyond(_calls(), "call", Decimal("100"), "up") == Decimal("105")

    def test_down(self):
        assert next_strike_beyond(_calls(), "call", Decimal("100"), "down") == Decimal("95")

    def test_none_when_reference_is_chain_edge(self):
        assert next_strike_beyond(_calls(), "call", Decimal("110"), "up") is None
        assert next_strike_beyond(_calls(), "call", Decimal("90"), "down") is None

    def test_none_when_no_strikes_on_that_side(self):
        assert next_strike_beyond([], "call", Decimal("100"), "up") is None
