"""V4.3 real-V3-decision strike replay tests (2026-09-02) -- the
general-purpose replay function, exercised against synthetic inputs.
The real 15-ticker dataset used for the V4.3 report lives in
test_v4_3_v3_strike_regression_fixtures.py, mirroring V4.2's own
test_v4_v3_regression_fixtures.py convention of keeping real,
DB-queried fixture data in its own file."""

from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.decision.v4_replay_strikes import V3StrikeReplayInput, replay_v3_strikes
from providers.types import OptionQuote

EXP = date(2026, 9, 18)
NOW = datetime(2026, 8, 17, tzinfo=UTC)


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


def _chain() -> tuple[OptionQuote, ...]:
    strikes = [Decimal(s) for s in (80, 85, 90, 95, 100, 105, 110, 115, 120)]
    quotes = []
    for k in strikes:
        distance = abs(k - Decimal(100))
        mid = max(Decimal("0.05"), Decimal(5) - distance * Decimal("0.3"))
        quotes.append(_quote(k, "call", mid - Decimal("0.05"), mid + Decimal("0.05")))
        quotes.append(_quote(k, "put", mid - Decimal("0.05"), mid + Decimal("0.05")))
    return tuple(quotes)


def test_no_action_decision_is_skipped_not_scored():
    decision = V3StrikeReplayInput(
        ticker="ZZ",
        strategy_type=None,
        underlying_price=Decimal("100"),
        observed_at=NOW,
        expiration=EXP,
        v3_legs=(),
        chain_quotes=_chain(),
        historical_next_day_move_pcts=None,
    )
    result = replay_v3_strikes(decision)
    assert result.replayable is False
    assert result.v4_result is None
    assert "NO_ACTION" in result.skip_reason


def test_unrecognized_strategy_is_flagged_not_guessed():
    decision = V3StrikeReplayInput(
        ticker="ZZ",
        strategy_type="not_a_real_strategy",
        underlying_price=Decimal("100"),
        observed_at=NOW,
        expiration=EXP,
        v3_legs=(),
        chain_quotes=_chain(),
        historical_next_day_move_pcts=None,
    )
    result = replay_v3_strikes(decision)
    assert result.replayable is False
    assert "Unrecognized strategy_type" in result.skip_reason


def test_missing_underlying_price_is_cannot_replay_honestly():
    decision = V3StrikeReplayInput(
        ticker="HPQ",
        strategy_type="long_call",
        underlying_price=None,
        observed_at=None,
        expiration=EXP,
        v3_legs=(),
        chain_quotes=_chain(),
        historical_next_day_move_pcts=None,
    )
    result = replay_v3_strikes(decision)
    assert result.replayable is False
    assert "CANNOT_REPLAY_HONESTLY" in result.skip_reason


def test_missing_chain_is_cannot_replay_honestly():
    decision = V3StrikeReplayInput(
        ticker="ZZ",
        strategy_type="long_call",
        underlying_price=Decimal("100"),
        observed_at=NOW,
        expiration=EXP,
        v3_legs=(),
        chain_quotes=None,
        historical_next_day_move_pcts=None,
    )
    result = replay_v3_strikes(decision)
    assert result.replayable is False
    assert "CANNOT_REPLAY_HONESTLY" in result.skip_reason


def test_successful_replay_runs_the_real_engine():
    decision = V3StrikeReplayInput(
        ticker="ZZ",
        strategy_type="iron_condor",
        underlying_price=Decimal("100"),
        observed_at=NOW,
        expiration=EXP,
        v3_legs=(("sell", "put", Decimal("95")), ("sell", "call", Decimal("105"))),
        chain_quotes=_chain(),
        historical_next_day_move_pcts=(Decimal("0.05"),) * 6,
    )
    result = replay_v3_strikes(decision)
    assert result.replayable is True
    assert result.v4_result is not None
    assert result.v4_result.status == "constructed"
    assert result.v3_legs == (("sell", "put", Decimal("95")), ("sell", "call", Decimal("105")))


def test_never_reads_a_realized_outcome_field():
    """Structural anti-lookahead guarantee -- the input dataclass has
    no field that could carry a realized outcome, so this rule can
    never be violated by construction (mirrors v4_replay.py's own
    Section 19 guarantee)."""
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(V3StrikeReplayInput)}
    forbidden = {"realized_move", "pnl", "settlement", "exit_price", "outcome"}
    assert field_names.isdisjoint(forbidden)
