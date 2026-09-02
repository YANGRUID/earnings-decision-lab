"""Options Decision Engine V4.2 -- Market View Model (2026-09-01).

Audits, then formalizes, exactly what V3's real DeepSeek DecisionView
schema commits its ``direction``/``volatility_view`` fields to mean --
quoted verbatim from ``schemas/decision.py`` below, never inferred from
the field names alone (this task's own explicit Section 3 instruction).

    direction: "The directional view best supported by the real evidence
    provided -- strong_bullish/bullish/neutral/bearish/strong_bearish.
    Choose neutral when the evidence is genuinely mixed or insufficient
    rather than guessing a direction."

    volatility_view: "Whether the real evidence supports expecting a
    larger-than-typical move (long_vol), a smaller-than-typical move
    (short_vol), or no strong view either way (neutral_vol). Grounded in
    the real historical move data and options-implied move provided, not
    a guess."

The volatility_view field's OWN description already defines it entirely
in terms of expected MOVE MAGNITUDE ("larger-than-typical move" /
"smaller-than-typical move") -- not implied-volatility level, not
"volatility staying elevated," not any other reading. This makes the
volatility_view -> expected-move-intent mapping below DETERMINISTIC and
directly evidenced by the real production prompt, not a V4 invention:
LONG_VOL means large_move, SHORT_VOL means small_move, by the schema's
own words. NEUTRAL_VOL explicitly commits to "no strong view either
way" -- it does NOT say small or large, so it honestly maps to
UNSPECIFIED, never guessed.

THIS MODULE DOES NOT CHANGE THE PROMPT. schemas/decision.py and
prompts/decision_view.py are read here, never edited (Section 4 of this
task is explicit: do not rewrite the production V3 prompt). This is a
pure, read-only interpretation layer over whatever DecisionView already
returned.
"""

from dataclasses import dataclass
from typing import Literal

from models.enums import DecisionDirection, DecisionVolatilityView

ExpectedMoveIntent = Literal["large_move", "moderate_move", "small_move", "unspecified"]

# Deterministic, evidenced directly by DecisionView.volatility_view's own
# field description (quoted in this module's docstring) -- not a V4
# guess. NEUTRAL_VOL is honestly UNSPECIFIED: the real prompt commits it
# to "no strong view either way," never a magnitude.
_MOVE_INTENT_BY_VOLATILITY_VIEW: dict[DecisionVolatilityView, ExpectedMoveIntent] = {
    DecisionVolatilityView.LONG_VOL: "large_move",
    DecisionVolatilityView.SHORT_VOL: "small_move",
    DecisionVolatilityView.NEUTRAL_VOL: "unspecified",
}


@dataclass(frozen=True)
class V4MarketView:
    """The V4.2 interpretation of one real (or hypothetical, for
    testing) DecisionView -- direction and volatility_view are carried
    through verbatim; expected_move_intent is derived only where the
    real prompt semantics make that derivation honest (see this module's
    docstring), UNSPECIFIED otherwise. Never fabricated."""

    direction: DecisionDirection
    volatility_view: DecisionVolatilityView | None
    expected_move_intent: ExpectedMoveIntent


def derive_v4_market_view(
    direction: DecisionDirection, volatility_view: DecisionVolatilityView | None
) -> V4MarketView:
    """``volatility_view`` is Optional because real, pre-Phase-4 V3
    DecisionSnapshot rows (e.g. DY, ZM, INTU -- generated before that
    field existed) genuinely have none on record. Never backfilled or
    guessed here -- a missing volatility_view honestly yields
    expected_move_intent="unspecified", exactly like NEUTRAL_VOL does."""
    move_intent: ExpectedMoveIntent = (
        _MOVE_INTENT_BY_VOLATILITY_VIEW[volatility_view]
        if volatility_view is not None
        else "unspecified"
    )
    return V4MarketView(
        direction=direction, volatility_view=volatility_view, expected_move_intent=move_intent
    )
