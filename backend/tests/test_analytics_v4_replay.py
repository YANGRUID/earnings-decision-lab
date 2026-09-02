"""V4.2 replay of real V3 decisions (2026-09-01) -- this task's own
Sections 16-18. All 23 real DecisionSnapshot rows ever created, queried
once, read-only, directly from the production database (never modified,
never re-queried by this test -- the real values are hardcoded here as a
fixture, matching this project's own established pattern for regression
tests over real historical rows, e.g. test_v4_v3_regression_fixtures.py).

ANTI-LOOKAHEAD (Section 19): only decision-time fields are used --
ticker, strategy_direction, volatility_view, strategy_type. No realized
move, P&L, settlement outcome, or post-event IV appears anywhere in this
file or in analytics/decision/v4_replay.py's own return type.
"""

from analytics.decision.v4_replay import V3DecisionReplayInput, replay_many
from models.enums import DecisionDirection, DecisionVolatilityView

# The real, complete 23-row V3 dataset (decision_snapshot.ticker,
# strategy_direction, volatility_view, strategy_type), in the real order
# generated. strategy_type is None for a genuine, real NO_ACTION
# decision (legs is JSON null on the real row).
REAL_V3_DECISIONS: list[V3DecisionReplayInput] = [
    V3DecisionReplayInput("DY", DecisionDirection.NEUTRAL, None, "long_call_butterfly"),
    V3DecisionReplayInput("ZM", DecisionDirection.NEUTRAL, None, "long_put"),
    V3DecisionReplayInput("INTU", DecisionDirection.NEUTRAL, None, "long_call_butterfly"),
    V3DecisionReplayInput("HEI", DecisionDirection.NEUTRAL, None, "long_call_butterfly"),
    V3DecisionReplayInput("SMTC", DecisionDirection.NEUTRAL, None, "long_call_butterfly"),
    V3DecisionReplayInput("WSM", DecisionDirection.NEUTRAL, None, "long_call_butterfly"),
    V3DecisionReplayInput("SJM", DecisionDirection.NEUTRAL, None, None),
    V3DecisionReplayInput("DCI", DecisionDirection.NEUTRAL, None, "long_call_butterfly"),
    V3DecisionReplayInput(
        "VEEV", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, "long_call_butterfly"
    ),
    V3DecisionReplayInput(
        "CRM", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, "long_call_butterfly"
    ),
    V3DecisionReplayInput(
        "HRL", DecisionDirection.NEUTRAL, DecisionVolatilityView.SHORT_VOL, "iron_condor"
    ),
    V3DecisionReplayInput(
        "HPQ", DecisionDirection.NEUTRAL, DecisionVolatilityView.NEUTRAL_VOL, "bear_put_spread"
    ),
    V3DecisionReplayInput("P", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, None),
    V3DecisionReplayInput(
        "NVDA", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, "long_call_butterfly"
    ),
    V3DecisionReplayInput(
        "CRWD", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, "long_put"
    ),
    V3DecisionReplayInput(
        "SNPS", DecisionDirection.BEARISH, DecisionVolatilityView.NEUTRAL_VOL, "bear_put_spread"
    ),
    V3DecisionReplayInput("A", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, None),
    V3DecisionReplayInput(
        "DG", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, "iron_condor"
    ),
    V3DecisionReplayInput(
        "DLTR", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, "long_call_butterfly"
    ),
    V3DecisionReplayInput("ADSK", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, None),
    V3DecisionReplayInput("MRVL", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, None),
    V3DecisionReplayInput("WDAY", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, None),
    V3DecisionReplayInput("AFRM", DecisionDirection.NEUTRAL, DecisionVolatilityView.LONG_VOL, None),
]

# The 7 real settled trades (forensic audit) among the 23 above.
SETTLED_TICKERS = {"DY", "VEEV", "CRM", "HPQ", "NVDA", "DG", "DLTR"}


def _result_for(ticker: str):
    results = replay_many(REAL_V3_DECISIONS)
    matches = [r for r in results if r.ticker == ticker]
    assert len(matches) == 1
    return matches[0]


def test_replay_covers_all_23_real_decisions():
    results = replay_many(REAL_V3_DECISIONS)
    assert len(results) == 23
    assert {r.ticker for r in results} == {d.ticker for d in REAL_V3_DECISIONS}


def test_no_action_decisions_are_skipped_not_scored():
    for ticker in ("SJM", "P", "A", "ADSK", "MRVL", "WDAY", "AFRM"):
        result = _result_for(ticker)
        assert result.compatibility is None
        assert result.skip_reason is not None
        assert "NO_ACTION" in result.skip_reason


class TestTheThreeRealLongVolButterflyContradictions:
    """Section 18's own explicit ask: CRM, VEEV, NVDA."""

    def test_crm(self):
        r = _result_for("CRM")
        assert r.v3_selected_strategy == "long_call_butterfly"
        assert r.compatibility is not None
        assert r.compatibility.overall_semantic_compatibility <= 0.25
        assert "MOVE_INTENT_CONTRADICTION" in r.compatibility.reason_codes
        assert "VOLATILITY_CONTRADICTION" in r.compatibility.reason_codes

    def test_veev(self):
        r = _result_for("VEEV")
        assert r.compatibility.overall_semantic_compatibility <= 0.25
        assert "MOVE_INTENT_CONTRADICTION" in r.compatibility.reason_codes

    def test_nvda(self):
        r = _result_for("NVDA")
        assert r.compatibility.overall_semantic_compatibility <= 0.25
        assert "MOVE_INTENT_CONTRADICTION" in r.compatibility.reason_codes

    def test_a_fourth_long_vol_butterfly_dltr_is_the_same_contradiction(self):
        """DLTR is the 4th real LONG_VOL butterfly among the settled
        cohort -- the same contradiction, not called out by name in the
        forensic audit's headline but real in the data."""
        r = _result_for("DLTR")
        assert r.compatibility.overall_semantic_compatibility <= 0.25


class TestRemainingSettledTrades:
    """DY, HPQ, DG -- Section 18's explicit ask."""

    def test_dy_is_conditional_not_a_contradiction_missing_volatility_view_honestly(self):
        """DY predates the volatility_view field -- V4.2 must not
        invent one; the honest result is 'conditional', not a false
        strong OR a false contradiction."""
        r = _result_for("DY")
        assert r.v3_volatility_view is None
        assert r.compatibility is not None
        assert r.compatibility.overall_semantic_compatibility == 0.5
        assert "MARKET_VIEW_UNDERSPECIFIED" in r.compatibility.reason_codes

    def test_hpq_is_poor_driven_by_a_real_direction_mismatch(self):
        """HPQ's own strategy_direction is NEUTRAL, but the selected
        bear_put_spread has directional_intent=bearish -- a real
        mismatch V4.2 catches independently, cross-validated by V3's
        own real direction_fit score for HPQ (4/15, itself low)."""
        r = _result_for("HPQ")
        assert r.compatibility is not None
        assert r.compatibility.overall_semantic_compatibility <= 0.25
        assert r.compatibility.direction_compatibility <= 0.25

    def test_dg_iron_condor_is_a_real_volatility_contradiction_under_long_vol(self):
        """DG's real V3 volatility_fit score was already 0/11 (V3's own
        sign-based check happens to catch this one correctly, since
        iron_condor genuinely is net credit) -- V4.2 independently
        confirms the same contradiction via real payoff geometry."""
        r = _result_for("DG")
        assert r.compatibility is not None
        assert r.compatibility.overall_semantic_compatibility <= 0.25
        assert "VOLATILITY_CONTRADICTION" in r.compatibility.reason_codes


def test_hrl_the_one_well_matched_settled_family_scores_strong():
    """HRL (not itself settled, but a real captured entry) is the one
    real case where volatility_view (SHORT_VOL) and strategy
    (iron_condor) genuinely agree -- V4.2 should show this positively,
    not just find contradictions everywhere."""
    r = _result_for("HRL")
    assert r.compatibility is not None
    assert r.compatibility.overall_semantic_compatibility == 1.0


def test_zero_of_seven_settled_trades_score_good_or_strong():
    """The real, striking, non-fitted headline result: every one of the
    7 real settled trades' selected strategy is semantically
    conditional, poor, or an outright contradiction under V4.2 -- none
    score 'good' or 'strong'."""
    results = {r.ticker: r for r in replay_many(REAL_V3_DECISIONS) if r.ticker in SETTLED_TICKERS}
    assert len(results) == 7
    for ticker, result in results.items():
        assert result.compatibility is not None, ticker
        assert result.compatibility.overall_semantic_compatibility < 0.75, (
            f"{ticker} unexpectedly scored good/strong: "
            f"{result.compatibility.overall_semantic_compatibility}"
        )


class TestNeutralBiasDiagnostic:
    """Section 16 -- a read-only diagnostic over the real 23-decision
    dataset, quantifying where the NEUTRAL concentration comes from.
    Never changes production behavior; locks in the real distribution as
    a regression fact."""

    def test_llm_view_distribution_is_overwhelmingly_neutral(self):
        """Part A -- the LLM view distribution itself."""
        directions = [d.direction for d in REAL_V3_DECISIONS]
        neutral_count = sum(1 for d in directions if d == DecisionDirection.NEUTRAL)
        bearish_count = sum(1 for d in directions if d == DecisionDirection.BEARISH)
        bullish_count = sum(1 for d in directions if d == DecisionDirection.BULLISH)
        assert neutral_count == 22
        assert bearish_count == 1
        assert bullish_count == 0
        assert len(directions) == 23

    def test_neutral_range_strategies_dominate_actionable_decisions_regardless_of_view(self):
        """Part B -- the strategy-ranking effect: neutral/range-shaped
        structures (butterflies + iron condors) dominate the actual
        selections even though only 1 of 23 real views was ever
        non-NEUTRAL, and even among the LONG_VOL-labeled subset (which,
        per V4.2 semantics, should have favored two-sided-convex
        structures like straddles/strangles instead)."""
        actionable = [d for d in REAL_V3_DECISIONS if d.strategy_type]
        assert len(actionable) == 16
        neutral_range_types = {"long_call_butterfly", "iron_condor", "iron_butterfly"}
        neutral_range_count = sum(1 for d in actionable if d.strategy_type in neutral_range_types)
        # 10 long_call_butterfly + 2 iron_condor = 12 of 16 (75%).
        assert neutral_range_count == 12
        # Not one real long_straddle or long_strangle was EVER selected,
        # despite 12 of the 16 actionable decisions carrying a LONG_VOL
        # view -- the exact V4.2 semantic gap this task closes.
        assert (
            sum(1 for d in actionable if d.strategy_type in ("long_straddle", "long_strangle")) == 0
        )
        long_vol_actionable = [
            d for d in actionable if d.volatility_view == DecisionVolatilityView.LONG_VOL
        ]
        assert len(long_vol_actionable) == 6  # VEEV, CRM, NVDA, CRWD, DG, DLTR
        long_vol_butterflies = sum(
            1 for d in long_vol_actionable if d.strategy_type == "long_call_butterfly"
        )
        assert long_vol_butterflies == 4  # VEEV, CRM, NVDA, DLTR


def test_no_result_carries_any_realized_outcome_field():
    """Structural anti-lookahead confirmation (Section 19): the return
    type itself has no P&L/settlement/realized-move field to accidentally
    populate."""
    from dataclasses import fields

    from analytics.decision.v4_replay import V3DecisionReplayResult

    field_names = {f.name for f in fields(V3DecisionReplayResult)}
    for forbidden in ("realized_pnl", "r_multiple", "is_win", "realized_move", "settlement"):
        assert forbidden not in field_names
