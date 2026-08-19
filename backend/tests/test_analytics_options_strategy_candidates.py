from datetime import UTC, date, datetime
from decimal import Decimal

from analytics.options.strategy_candidates import StrategyCategory, generate_candidates
from providers.types import OptionQuote

EXP = date(2026, 9, 18)
OTHER_EXP = date(2026, 9, 25)
NOW = datetime(2026, 8, 17, tzinfo=UTC)
UNDERLYING = Decimal("100")


def _quote(
    strike: Decimal,
    option_type: str,
    bid: Decimal | None = None,
    ask: Decimal | None = None,
    expiration: date = EXP,
) -> OptionQuote:
    return OptionQuote(
        ticker="ZZSTRAT",
        snapshot_timestamp=NOW,
        expiration_date=expiration,
        strike=strike,
        option_type=option_type,
        bid=bid,
        ask=ask,
        source_provider="test",
        retrieved_at=NOW,
    )


def _wide_chain() -> list[OptionQuote]:
    """Five real strikes each side of ATM, both call and put, all priced --
    enough width for every strategy category. Extrinsic value decays with
    distance from ATM (like a real chain), so OTM strikes further from the
    money are honestly cheaper than nearer ones -- e.g. a real put credit
    spread here nets a real credit, not a coincidental wash."""
    strikes = [Decimal(s) for s in (85, 90, 95, 100, 105, 110, 115)]
    quotes = []
    for s in strikes:
        distance = abs(s - UNDERLYING) / Decimal(5)
        extrinsic = Decimal("2.0") - Decimal("0.3") * distance
        call_mid = max(UNDERLYING - s, Decimal(0)) + extrinsic
        put_mid = max(s - UNDERLYING, Decimal(0)) + extrinsic
        quotes.append(_quote(s, "call", call_mid - Decimal("0.10"), call_mid + Decimal("0.10")))
        quotes.append(_quote(s, "put", put_mid - Decimal("0.10"), put_mid + Decimal("0.10")))
    return quotes


def test_wide_chain_generates_every_category():
    candidates = generate_candidates(_wide_chain(), UNDERLYING, EXP)
    categories = {c.category for c in candidates}
    assert categories == set(StrategyCategory)


def test_long_call_uses_atm_strike_and_real_mid_premium():
    candidates = generate_candidates(_wide_chain(), UNDERLYING, EXP)
    long_call = next(c for c in candidates if c.category == StrategyCategory.LONG_CALL)
    assert len(long_call.legs) == 1
    assert long_call.legs[0].strike == Decimal("100")
    assert long_call.legs[0].premium == Decimal("2.00")  # mid of (1.90, 2.10)


def test_bull_call_spread_uses_atm_and_next_strike_up():
    candidates = generate_candidates(_wide_chain(), UNDERLYING, EXP)
    spread = next(c for c in candidates if c.category == StrategyCategory.BULL_CALL_SPREAD)
    strikes = sorted(leg.strike for leg in spread.legs)
    assert strikes == [Decimal("100"), Decimal("105")]


def test_put_credit_spread_sells_one_below_buys_two_below():
    candidates = generate_candidates(_wide_chain(), UNDERLYING, EXP)
    spread = next(c for c in candidates if c.category == StrategyCategory.PUT_CREDIT_SPREAD)
    strikes = sorted(leg.strike for leg in spread.legs)
    assert strikes == [Decimal("90"), Decimal("95")]
    # Net premium should come out as a credit (negative).
    assert spread.analysis.net_premium < 0


def test_iron_condor_uses_four_real_strikes_symmetric_around_atm():
    candidates = generate_candidates(_wide_chain(), UNDERLYING, EXP)
    condor = next(c for c in candidates if c.category == StrategyCategory.IRON_CONDOR)
    strikes = sorted(leg.strike for leg in condor.legs)
    assert strikes == [Decimal("90"), Decimal("95"), Decimal("105"), Decimal("110")]


def test_long_call_butterfly_uses_atm_and_one_strike_each_side():
    candidates = generate_candidates(_wide_chain(), UNDERLYING, EXP)
    fly = next(c for c in candidates if c.category == StrategyCategory.LONG_CALL_BUTTERFLY)
    # Three distinct leg objects -- the middle (short) leg is one leg with
    # quantity=2, not two separate leg objects.
    strikes = sorted(leg.strike for leg in fly.legs)
    assert strikes == [Decimal("95"), Decimal("100"), Decimal("105")]
    middle_leg = next(leg for leg in fly.legs if leg.strike == Decimal("100"))
    assert middle_leg.quantity == 2


def test_iron_butterfly_sells_atm_straddle_buys_one_strike_wings():
    candidates = generate_candidates(_wide_chain(), UNDERLYING, EXP)
    fly = next(c for c in candidates if c.category == StrategyCategory.IRON_BUTTERFLY)
    strikes = sorted(leg.strike for leg in fly.legs)
    assert strikes == [Decimal("95"), Decimal("100"), Decimal("100"), Decimal("105")]
    # Net premium should come out as a credit (negative) -- selling the ATM
    # straddle collects more than the OTM wings cost.
    assert fly.analysis.net_premium < 0


def test_iron_butterfly_requires_same_center_strike_on_both_sides():
    # Calls have no 100 strike (nearest-ATM call resolves to 95); puts do
    # have one (nearest-ATM put resolves to 100 exactly) -- an iron
    # butterfly needs one real shared center strike, same rule the long
    # straddle already uses, so it must be omitted rather than mixing a
    # 95-strike short call with a 100-strike short put.
    call_strikes = [Decimal(s) for s in (85, 90, 95, 105, 110, 115)]  # no 100
    put_strikes = [Decimal(s) for s in (85, 90, 100, 105, 110, 115)]  # has 100
    quotes = []
    for s in call_strikes:
        quotes.append(_quote(s, "call", Decimal("1.90"), Decimal("2.10")))
    for s in put_strikes:
        quotes.append(_quote(s, "put", Decimal("1.90"), Decimal("2.10")))
    candidates = generate_candidates(quotes, UNDERLYING, EXP)
    categories = {c.category for c in candidates}
    assert StrategyCategory.IRON_BUTTERFLY not in categories


def test_long_straddle_requires_same_strike_on_both_sides():
    candidates = generate_candidates(_wide_chain(), UNDERLYING, EXP)
    straddle = next(c for c in candidates if c.category == StrategyCategory.LONG_STRADDLE)
    strikes = {leg.strike for leg in straddle.legs}
    assert strikes == {Decimal("100")}


def test_narrow_chain_omits_categories_needing_wider_wings():
    # Only ATM +/- 1 strike on each side -- not enough width for a credit
    # spread's second protective leg (offset -2/+2) or the iron condor.
    strikes = [Decimal(s) for s in (95, 100, 105)]
    quotes = []
    for s in strikes:
        quotes.append(_quote(s, "call", Decimal("1.90"), Decimal("2.10")))
        quotes.append(_quote(s, "put", Decimal("1.90"), Decimal("2.10")))

    candidates = generate_candidates(quotes, UNDERLYING, EXP)
    categories = {c.category for c in candidates}

    assert StrategyCategory.LONG_CALL in categories
    assert StrategyCategory.LONG_PUT in categories
    assert StrategyCategory.BULL_CALL_SPREAD in categories
    assert StrategyCategory.BEAR_PUT_SPREAD in categories
    assert StrategyCategory.LONG_STRADDLE in categories
    assert StrategyCategory.LONG_STRANGLE in categories
    # The butterflies only need +/-1 strike of width (same as the strangle),
    # so a 3-strike chain is enough for them, unlike the credit spreads and
    # iron condor below (which need +/-2).
    assert StrategyCategory.LONG_CALL_BUTTERFLY in categories
    assert StrategyCategory.IRON_BUTTERFLY in categories
    # Not enough width for these -- never fabricated with a made-up strike.
    assert StrategyCategory.PUT_CREDIT_SPREAD not in categories
    assert StrategyCategory.CALL_CREDIT_SPREAD not in categories
    assert StrategyCategory.IRON_CONDOR not in categories


def test_unpriceable_quote_is_skipped_not_fabricated():
    # ATM call has no bid/ask/last at all -- every category needing it
    # (long_call, bull_call_spread, straddle, strangle) must be omitted.
    strikes = [Decimal(s) for s in (90, 95, 100, 105, 110)]
    quotes = []
    for s in strikes:
        if s == Decimal("100"):
            quotes.append(_quote(s, "call", None, None))  # no real price available
        else:
            quotes.append(_quote(s, "call", Decimal("1.90"), Decimal("2.10")))
        quotes.append(_quote(s, "put", Decimal("1.90"), Decimal("2.10")))

    candidates = generate_candidates(quotes, UNDERLYING, EXP)
    categories = {c.category for c in candidates}

    assert StrategyCategory.LONG_CALL not in categories
    assert StrategyCategory.LONG_STRADDLE not in categories
    assert StrategyCategory.LONG_PUT in categories  # puts are unaffected


def test_only_matching_expiration_quotes_are_considered():
    strikes = [Decimal(s) for s in (95, 100, 105)]
    quotes = []
    for s in strikes:
        quotes.append(_quote(s, "call", Decimal("1.90"), Decimal("2.10"), expiration=OTHER_EXP))
        quotes.append(_quote(s, "put", Decimal("1.90"), Decimal("2.10"), expiration=OTHER_EXP))

    candidates = generate_candidates(quotes, UNDERLYING, EXP)
    assert candidates == []


def test_empty_chain_returns_no_candidates():
    assert generate_candidates([], UNDERLYING, EXP) == []


def test_each_candidate_carries_a_real_deterministic_analysis():
    candidates = generate_candidates(_wide_chain(), UNDERLYING, EXP)
    long_call = next(c for c in candidates if c.category == StrategyCategory.LONG_CALL)
    # Long call: max loss = premium paid, max profit unbounded.
    assert long_call.analysis.max_loss == Decimal("2.00")
    assert long_call.analysis.max_profit is None
    assert long_call.analysis.breakevens == (Decimal("102.00"),)
