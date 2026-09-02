"""V4.3 expected-move context tests (2026-09-02). Verifies the ONE
authoritative ExpectedMoveContext object honestly represents implied
move, historical move distribution, and evidence-quality tiers --
never fabricating a number when a real signal is absent (Section 2)."""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_expected_move import (
    MIN_N_FOR_DECILES,
    MIN_N_FOR_QUARTILES,
    derive_expected_move_context,
)
from providers.types import OptionQuote

EXP = date(2026, 9, 18)
NOW = datetime(2026, 8, 17, tzinfo=UTC)
SPOT = Decimal("100")


def _quote(strike: Decimal, option_type: str, bid: Decimal, ask: Decimal) -> OptionQuote:
    return OptionQuote(
        ticker="ZZ",
        snapshot_timestamp=NOW,
        expiration_date=EXP,
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        source_provider="test",
        retrieved_at=NOW,
    )


def _atm_straddle_quotes() -> list[OptionQuote]:
    return [
        _quote(SPOT, "call", Decimal("4.90"), Decimal("5.10")),
        _quote(SPOT, "put", Decimal("4.90"), Decimal("5.10")),
    ]


class TestImpliedMove:
    def test_available_from_atm_straddle(self):
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=EXP,
            quotes_for_expiration=_atm_straddle_quotes(),
            historical_next_day_move_pcts=None,
        )
        assert ctx.implied_move_available is True
        assert ctx.implied_move_dollars == Decimal("10.00")
        assert ctx.implied_move_pct == Decimal("0.10")
        assert ctx.upper_implied_boundary == Decimal("110.00")
        assert ctx.lower_implied_boundary == Decimal("90.00")
        assert ctx.implied_move_source == "atm_straddle"

    def test_unavailable_when_no_quotes(self):
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=EXP,
            quotes_for_expiration=None,
            historical_next_day_move_pcts=None,
        )
        assert ctx.implied_move_available is False
        assert ctx.implied_move_dollars is None
        assert ctx.upper_implied_boundary is None
        assert ctx.lower_implied_boundary is None
        assert ctx.implied_move_source == "unavailable"

    def test_unavailable_when_expiration_missing(self):
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=None,
            quotes_for_expiration=_atm_straddle_quotes(),
            historical_next_day_move_pcts=None,
        )
        assert ctx.implied_move_available is False

    def test_unavailable_when_only_call_side_quoted(self):
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=EXP,
            quotes_for_expiration=[_quote(SPOT, "call", Decimal("4.90"), Decimal("5.10"))],
            historical_next_day_move_pcts=None,
        )
        assert ctx.implied_move_available is False


class TestHistoricalMoveDistribution:
    def test_insufficient_with_zero_observations(self):
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=None,
            quotes_for_expiration=None,
            historical_next_day_move_pcts=[],
        )
        assert ctx.historical_evidence_quality == "insufficient"
        assert ctx.historical_sample_n == 0
        assert ctx.historical_median_abs_move_pct is None
        assert ctx.historical_quantiles is None

    def test_limited_below_quartile_minimum(self):
        moves = [Decimal("0.05")] * (MIN_N_FOR_QUARTILES - 1)
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=None,
            quotes_for_expiration=None,
            historical_next_day_move_pcts=moves,
        )
        assert ctx.historical_evidence_quality == "limited"
        assert ctx.historical_median_abs_move_pct is not None  # real median still shown
        assert ctx.historical_quantiles is None  # but no false-precision quartiles

    def test_adequate_quartiles_below_decile_minimum(self):
        moves = [Decimal(str(v)) for v in [0.03, 0.05, 0.04, 0.06, 0.07]]
        assert MIN_N_FOR_QUARTILES <= len(moves) < MIN_N_FOR_DECILES
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=None,
            quotes_for_expiration=None,
            historical_next_day_move_pcts=moves,
        )
        assert ctx.historical_evidence_quality == "adequate_quartiles"
        assert ctx.historical_quantiles is not None
        assert ctx.historical_quantiles.p10_abs_move_pct is None  # no false-precision decile
        assert ctx.historical_quantiles.p90_abs_move_pct is None

    def test_adequate_deciles_at_or_above_decile_minimum(self):
        moves = [Decimal(str(0.03 + 0.005 * i)) for i in range(MIN_N_FOR_DECILES)]
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=None,
            quotes_for_expiration=None,
            historical_next_day_move_pcts=moves,
        )
        assert ctx.historical_evidence_quality == "adequate_deciles"
        assert ctx.historical_quantiles is not None
        assert ctx.historical_quantiles.p10_abs_move_pct is not None
        assert ctx.historical_quantiles.p90_abs_move_pct is not None

    def test_median_boundaries_derived_from_spot(self):
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=None,
            quotes_for_expiration=None,
            historical_next_day_move_pcts=[
                Decimal("0.05"),
                Decimal("0.05"),
                Decimal("0.05"),
                Decimal("0.05"),
            ],
        )
        assert ctx.historical_median_abs_move_pct == Decimal("0.05")
        assert ctx.historical_median_upper_boundary == Decimal("105.00")
        assert ctx.historical_median_lower_boundary == Decimal("95.00")

    def test_quantiles_never_extrapolate_beyond_observed_range(self):
        moves = [Decimal(str(v)) for v in [0.02, 0.04, 0.06, 0.08, 0.10]]
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=None,
            quotes_for_expiration=None,
            historical_next_day_move_pcts=moves,
        )
        q = ctx.historical_quantiles
        assert q is not None
        assert min(moves) <= q.p25_abs_move_pct <= max(moves)
        assert min(moves) <= q.p75_abs_move_pct <= max(moves)


class TestNoFabrication:
    def test_bare_context_has_no_signal_at_all(self):
        ctx = derive_expected_move_context(
            spot=SPOT,
            observed_at=NOW,
            expiration=None,
            quotes_for_expiration=None,
            historical_next_day_move_pcts=None,
        )
        assert ctx.implied_move_available is False
        assert ctx.historical_evidence_quality == "insufficient"
        assert ctx.historical_sample_n == 0
