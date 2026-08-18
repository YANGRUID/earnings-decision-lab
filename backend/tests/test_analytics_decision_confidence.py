from decimal import Decimal

from analytics.decision.confidence import compute_confidence
from models.enums import DecisionDirection, RevisionDirection


def test_full_evidence_and_agreement_scores_high():
    result = compute_confidence(
        direction=DecisionDirection.BULLISH,
        evidence_tool_success_count=4,
        evidence_tool_total=4,
        revision_direction=RevisionDirection.UP,
        historical_move_pcts=[Decimal("0.05"), Decimal("0.03"), Decimal("0.02")],
        snapshot_age_minutes=10,
        chain_exists=True,
        implied_move_available=True,
    )
    assert result.total >= 90


def test_no_evidence_or_data_scores_low():
    result = compute_confidence(
        direction=DecisionDirection.BULLISH,
        evidence_tool_success_count=0,
        evidence_tool_total=4,
        revision_direction=None,
        historical_move_pcts=[],
        snapshot_age_minutes=None,
        chain_exists=False,
        implied_move_available=False,
    )
    assert result.total < 20
    assert result.evidence_coverage == 0
    assert result.data_freshness == 0
    assert result.options_completeness == 0


def test_consensus_revisions_opposite_direction_score_zero():
    result = compute_confidence(
        direction=DecisionDirection.STRONG_BULLISH,
        evidence_tool_success_count=4,
        evidence_tool_total=4,
        revision_direction=RevisionDirection.DOWN,
        historical_move_pcts=[],
        snapshot_age_minutes=10,
        chain_exists=True,
        implied_move_available=True,
    )
    assert result.consensus_agreement == 0


def test_historical_consistency_all_moves_match_bullish_direction():
    result = compute_confidence(
        direction=DecisionDirection.BULLISH,
        evidence_tool_success_count=4,
        evidence_tool_total=4,
        revision_direction=None,
        historical_move_pcts=[Decimal("0.05"), Decimal("0.03"), Decimal("0.02")],
        snapshot_age_minutes=10,
        chain_exists=True,
        implied_move_available=True,
    )
    assert result.historical_consistency == 20  # full weight -- 3/3 positive moves


def test_historical_consistency_all_moves_contradict_bearish_direction():
    result = compute_confidence(
        direction=DecisionDirection.BEARISH,
        evidence_tool_success_count=4,
        evidence_tool_total=4,
        revision_direction=None,
        historical_move_pcts=[Decimal("0.05"), Decimal("0.03"), Decimal("0.02")],
        snapshot_age_minutes=10,
        chain_exists=True,
        implied_move_available=True,
    )
    assert result.historical_consistency == 0  # all positive moves, direction is bearish


def test_neutral_direction_favors_small_historical_moves():
    result = compute_confidence(
        direction=DecisionDirection.NEUTRAL,
        evidence_tool_success_count=4,
        evidence_tool_total=4,
        revision_direction=RevisionDirection.FLAT,
        historical_move_pcts=[Decimal("0.01"), Decimal("0.02")],
        snapshot_age_minutes=10,
        chain_exists=True,
        implied_move_available=True,
    )
    assert result.historical_consistency == 20  # both moves <= 3%
    assert result.consensus_agreement == 20  # flat revisions match neutral view


def test_stale_snapshot_reduces_data_freshness_score():
    fresh = compute_confidence(
        direction=DecisionDirection.NEUTRAL,
        evidence_tool_success_count=4,
        evidence_tool_total=4,
        revision_direction=None,
        historical_move_pcts=[],
        snapshot_age_minutes=30,
        chain_exists=True,
        implied_move_available=True,
    )
    stale = compute_confidence(
        direction=DecisionDirection.NEUTRAL,
        evidence_tool_success_count=4,
        evidence_tool_total=4,
        revision_direction=None,
        historical_move_pcts=[],
        snapshot_age_minutes=60 * 24 * 30,  # a month old
        chain_exists=True,
        implied_move_available=True,
    )
    assert fresh.data_freshness > stale.data_freshness
    assert stale.data_freshness == 0


def test_chain_exists_but_no_implied_move_scores_half_options_completeness():
    result = compute_confidence(
        direction=DecisionDirection.NEUTRAL,
        evidence_tool_success_count=4,
        evidence_tool_total=4,
        revision_direction=None,
        historical_move_pcts=[],
        snapshot_age_minutes=10,
        chain_exists=True,
        implied_move_available=False,
    )
    assert 0 < result.options_completeness < 20


def test_as_dict_matches_dataclass_fields():
    result = compute_confidence(
        direction=DecisionDirection.NEUTRAL,
        evidence_tool_success_count=2,
        evidence_tool_total=4,
        revision_direction=None,
        historical_move_pcts=[],
        snapshot_age_minutes=10,
        chain_exists=True,
        implied_move_available=True,
    )
    d = result.as_dict()
    assert d["evidence_coverage"] == result.evidence_coverage
    assert set(d.keys()) == {
        "evidence_coverage",
        "consensus_agreement",
        "historical_consistency",
        "data_freshness",
        "options_completeness",
    }


def test_total_never_exceeds_100():
    result = compute_confidence(
        direction=DecisionDirection.STRONG_BULLISH,
        evidence_tool_success_count=4,
        evidence_tool_total=4,
        revision_direction=RevisionDirection.UP,
        historical_move_pcts=[Decimal("0.05")] * 10,
        snapshot_age_minutes=1,
        chain_exists=True,
        implied_move_available=True,
    )
    assert result.total <= 100
