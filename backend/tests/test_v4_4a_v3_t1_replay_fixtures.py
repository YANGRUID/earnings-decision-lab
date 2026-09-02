"""V4.4A -- real V3 decision T+1 scenario replay (2026-09-03).

Reuses the SAME real, DB-queried chain fixture data V4.3's own
test_v4_3_v3_strike_regression_fixtures.py already established (15
real tickers' real captured chains, including real per-strike IV where
it survives) -- never re-typed, never re-fetched. This module adds
ONLY the T+1-specific inputs (entry/exit timestamps, expected-move
context) on top of that same real data.

ENTRY/EXIT TIMESTAMP APPROXIMATION, DISCLOSED: the real fixture only
preserves each decision's chain-capture ``observed_at``, not a
separately-recorded real entry_timestamp/expected_exit_timestamp.
``entry_timestamp = observed_at`` and
``expected_exit_timestamp = observed_at + 1 day`` are therefore a
disclosed approximation of the real ~15:55 ET T+1 benchmark schedule,
not a fabricated new data point -- see the V4.4A report's own Section
Q for this same disclosure.

Real per-strike IV coverage is genuinely sparse for several of these
15 tickers (confirmed by the V4.3 audit -- e.g. only 1 of 22 real CRM
chain rows carries a real IV) -- most of these real candidates are
therefore expected to land on CANNOT_REPLAY_HONESTLY here, which is
itself the honest, real finding this test suite verifies, not a bug."""

from datetime import timedelta

from test_v4_3_v3_strike_regression_fixtures import _ALL, _to_quotes

from analytics.decision.v4_expected_move import derive_expected_move_context
from analytics.decision.v4_t1_replay import (
    CANNOT_REPLAY_HONESTLY,
    V3T1ReplayInput,
    replay_many_t1_scenarios,
)


def _build_t1_input(decision: dict, raw_chain: list, historical_moves: list) -> V3T1ReplayInput:
    quotes = _to_quotes(decision, raw_chain)
    entry_ts = decision["observed_at"]
    exit_ts = entry_ts + timedelta(days=1)
    expected_move_context = derive_expected_move_context(
        spot=decision["underlying_price"],
        observed_at=entry_ts,
        expiration=decision["selected_expiration"],
        quotes_for_expiration=list(quotes),
        historical_next_day_move_pcts=list(historical_moves),
    )
    return V3T1ReplayInput(
        ticker=decision["ticker"],
        strategy_type=decision["strategy_type"],
        underlying_price=decision["underlying_price"],
        entry_timestamp=entry_ts,
        expected_exit_timestamp=exit_ts,
        expiration=decision["selected_expiration"],
        v3_legs=decision["v3_legs"],
        chain_quotes=quotes,
        expected_move_context=expected_move_context,
    )


REAL_V3_T1_REPLAY_INPUTS: list[V3T1ReplayInput] = [
    _build_t1_input(decision, chain, moves) for decision, chain, moves in _ALL
]


def _results() -> dict[str, object]:
    return {r.ticker: r for r in replay_many_t1_scenarios(REAL_V3_T1_REPLAY_INPUTS)}


def test_all_fifteen_real_decisions_are_attempted():
    results = _results()
    assert len(results) == 15


def test_every_result_is_either_replayable_or_honestly_flagged():
    for ticker, result in _results().items():
        assert result.replayable is True or (
            result.skip_reason is not None
            and (
                CANNOT_REPLAY_HONESTLY in result.skip_reason or "Unrecognized" in result.skip_reason
            )
        ), ticker


def test_sparse_iv_coverage_produces_real_cannot_replay_honestly_cases():
    """The real, honest finding this replay demonstrates: several real
    tickers (confirmed by the V4.3 audit to have sparse real per-
    strike IV) cannot be honestly repriced -- not a defect, the
    correct behavior given real data gaps."""
    results = _results()
    cannot_replay = [
        t
        for t, r in results.items()
        if not r.replayable and r.skip_reason and CANNOT_REPLAY_HONESTLY in r.skip_reason
    ]
    assert len(cannot_replay) > 0


def test_no_replay_ever_imports_a_live_provider():
    """Structural anti-lookahead check: every real chain quote fed into
    the replay comes from _to_quotes, itself built entirely from the
    real, already-persisted, decision-time chain rows -- this test
    confirms v4_t1_replay.py never imports the real IBKR provider or
    any other live market-data source (only real `import`/`from`
    lines are checked, never an incidental docstring mention)."""
    import inspect

    import analytics.decision.v4_t1_replay as replay_module

    for line in inspect.getsource(replay_module).splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "ibkr" not in stripped.lower()
            assert "provider" not in stripped.lower() or "providers.types" in stripped


def test_replayable_candidates_produce_a_real_scenario_envelope():
    results = _results()
    replayable = [r for r in results.values() if r.replayable]
    for result in replayable:
        assert result.scenario_results is not None
        assert len(result.scenario_results) == 21  # 7 underlying x 3 IV
        assert result.distribution_summary is not None
        assert result.distribution_summary.n_valued > 0


def test_deterministic_repeatability_on_real_data():
    first = replay_many_t1_scenarios(REAL_V3_T1_REPLAY_INPUTS)
    second = replay_many_t1_scenarios(REAL_V3_T1_REPLAY_INPUTS)
    assert first == second


def test_no_action_tickers_are_not_in_this_fixture():
    """This fixture, like V4.3's own, only carries the 15 real
    actionable decisions -- the 7 real NO_ACTION tickers are correctly
    absent, not silently scored as CANNOT_REPLAY_HONESTLY."""
    tickers = set(_results().keys())
    no_action_tickers = {"SJM", "P", "A", "ADSK", "MRVL", "WDAY", "AFRM"}
    assert tickers.isdisjoint(no_action_tickers)


class TestSpecificRealTickers:
    """Named, individually-inspected results for the report's own
    Section L narrative -- real outcomes, whichever way they land."""

    def test_report_status_for_every_ticker(self):
        results = _results()
        for ticker in (
            "CRM",
            "VEEV",
            "NVDA",
            "DLTR",
            "DG",
            "HRL",
            "DY",
            "ZM",
            "CRWD",
            "SNPS",
            "INTU",
            "HEI",
            "SMTC",
            "WSM",
            "DCI",
        ):
            assert ticker in results
            r = results[ticker]
            status = "replayable" if r.replayable else r.skip_reason
            assert status  # just confirms every real ticker has SOME real, non-empty status
