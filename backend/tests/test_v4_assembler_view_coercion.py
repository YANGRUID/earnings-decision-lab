"""Live-found defect (activation dry-run, 2026-09-02): the assembler received
the DecisionView's plain strings and derive_v4_market_view raised on
``.value``. Every prior test mocked assembly, so the first real in-process
run was the first time this line executed with real inputs."""

from models.enums import DecisionDirection, DecisionVolatilityView
from services.v4_shadow_assembler import _coerce_enum


class TestBoundaryCoercion:
    def test_strings_from_the_decision_view_become_enum_members(self):
        assert _coerce_enum(DecisionDirection, "bullish") is DecisionDirection.BULLISH
        assert _coerce_enum(DecisionVolatilityView, "long_vol") is DecisionVolatilityView.LONG_VOL
        assert _coerce_enum(DecisionDirection, "BEARISH") is DecisionDirection.BEARISH

    def test_enum_and_none_pass_through(self):
        assert (
            _coerce_enum(DecisionDirection, DecisionDirection.NEUTRAL) is DecisionDirection.NEUTRAL
        )
        assert _coerce_enum(DecisionVolatilityView, None) is None

    def test_market_view_derivation_accepts_the_orchestrations_strings(self):
        from analytics.decision.v4_market_view import derive_v4_market_view

        view = derive_v4_market_view(
            _coerce_enum(DecisionDirection, "bullish"),
            _coerce_enum(DecisionVolatilityView, "long_vol"),
        )
        assert view is not None
