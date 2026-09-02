"""V4.1 methodology foundation (2026-08-31) -- locks the version-object
contract this task's Section 17 requires, and confirms V3/V4 engine
version strings can never collide."""

from analytics.decision.v4_methodology import (
    ENGINE_VERSION_V4,
    NOT_IMPLEMENTED,
    T1_POST_EARNINGS_LIQUIDATION_V1,
    V4_METHODOLOGY,
    V4_ROADMAP,
)
from models.enums import ExitPolicy

# The retired V3 engine's version string, kept as a literal: the V3 code is gone.
ENGINE_VERSION_V3 = "options-decision-engine-v3"


def test_v4_engine_version_is_distinct_from_v3():
    assert ENGINE_VERSION_V4 != ENGINE_VERSION_V3
    assert ENGINE_VERSION_V4 == "options-decision-engine-v4"
    assert ENGINE_VERSION_V3 == "options-decision-engine-v3"


def test_benchmark_objective_is_the_real_t1_liquidation_definition():
    obj = T1_POST_EARNINGS_LIQUIDATION_V1
    assert obj.exit_policy == ExitPolicy.FIRST_POST_EARNINGS_TRADING_DAY_CLOSE
    assert obj.entry_time_et.hour == 15
    assert obj.entry_time_et.minute == 55
    assert obj.exit_time_et.hour == 15
    assert obj.exit_time_et.minute == 55
    assert obj.pricing_side_long_open == "ASK"
    assert obj.pricing_side_short_open == "BID"
    assert obj.pricing_side_long_close == "BID"
    assert obj.pricing_side_short_close == "ASK"
    assert "expiration payoff" in obj.description.lower()
    assert "not" in obj.description.lower()


def test_v4_methodology_version_object_has_real_and_placeholder_fields():
    m = V4_METHODOLOGY
    assert m.engine_version == ENGINE_VERSION_V4
    assert m.benchmark_objective == "t1_liquidation_v1"
    assert m.capital_semantics == "standardized_per_decision_v1"
    # V4.2 bumped this from v1 -> v2 (the credit-spread move_intent
    # re-audit correction).
    assert m.strategy_semantics_version == "v2"
    assert m.view_strategy_compatibility_version == "view_strategy_compatibility_v1"
    # V4.3 makes this real -- analytics/decision/v4_strike_engine.py.
    # ranking_version/expiration_version remain deferred to V4.4/later:
    # V4.3/V4.3.1 are strike geometry only, never final candidate
    # selection.
    assert m.strike_engine_version == "expected_move_v1"
    # V4.3.1 -- the variant-set generator's own, separate version.
    # strike_engine_version above is UNCHANGED by V4.3.1 -- confirmed
    # by V4.3's own full test suite still passing unmodified.
    assert m.geometry_candidate_version == "geometry_candidate_v1"
    # V4.4A -- the T+1 scenario valuation engine's own, separate
    # version. strike_engine_version/geometry_candidate_version above
    # are UNCHANGED by V4.4A -- confirmed by V4.3/V4.3.1's own full
    # test suites still passing unmodified.
    assert m.t1_valuation_version == "t1_pricing_v1"
    # V4.4B makes ranking_version real (analytics/decision/v4_4b_ranking.py)
    # -- the first V4 phase permitted to ORDER candidates. This assertion
    # previously required NOT_IMPLEMENTED, which was correct right up until
    # that phase shipped; it is updated deliberately, not relaxed. The
    # frozen string is asserted verbatim so a silent rename fails here,
    # which matters because every replay already run is keyed to it.
    assert m.ranking_version == "v4-4b-t1-executable-ranking-v1"
    # Still genuinely deferred: V4.4B compares whatever expirations
    # candidate generation honestly supplies and deliberately did NOT
    # reintroduce V3's own expiration score (Section 20).
    assert m.expiration_version == NOT_IMPLEMENTED


def test_roadmap_orders_v4_1_first_and_leaves_unstarted_stages_not_implemented():
    stage_ids = [s.id for s in V4_ROADMAP]
    assert stage_ids[0] == "v4.1"
    assert (
        stage_ids.index("v4.2")
        < stage_ids.index("v4.3")
        < stage_ids.index("v4.3.1")
        < stage_ids.index("v4.4a")
        < stage_ids.index("v4.4b")
    )
    completed = {"v4.1", "v4.2", "v4.3", "v4.3.1", "v4.4a"}
    for stage in V4_ROADMAP:
        if stage.id in completed:
            assert stage.status == "complete"
        else:
            assert stage.status == NOT_IMPLEMENTED


def test_roadmap_has_no_duplicate_stage_ids():
    stage_ids = [s.id for s in V4_ROADMAP]
    assert len(stage_ids) == len(set(stage_ids))
