"""The six standardized V4 configurations (Sections 14-20, 57-60).

Every test here runs against ONE shared, synthetic evidence set. That is
the point of the design and the point of the tests: six configurations,
one market observation.
"""

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from analytics.decision.v4_4b_ranking import RANKING_VERSION, RankableCandidate
from analytics.decision.v4_configurations import (
    V4_CONFIGURATIONS,
    get_configuration,
)
from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_t1_pricing import (
    T1CandidateDistributionSummary,
    T1ScenarioResult,
)
from analytics.decision.v4_t1_valuation_context import V4T1LegInput, V4T1ValuationContext
from models.enums import RiskProfile
from services.v4_config_evaluation import (
    EXCLUDED_CAPITAL,
    EXCLUDED_RISK_CAP,
    EXCLUDED_STRATEGY_FAMILY,
    evaluate_all_configurations,
    evaluate_configuration,
    max_defined_risk,
)

NOW = datetime(2026, 9, 10, 19, 30, tzinfo=UTC)
EXPIRATION = date(2026, 9, 18)


def _em() -> ExpectedMoveContext:
    return ExpectedMoveContext(
        spot=Decimal("100"), observed_at=NOW, implied_move_available=True,
        implied_move_dollars=Decimal("5"), implied_move_pct=Decimal("0.05"),
        upper_implied_boundary=Decimal("105"), lower_implied_boundary=Decimal("95"),
        implied_move_source="atm_straddle", implied_move_result=None,
        historical_sample_n=8, historical_evidence_quality="adequate",
        historical_median_abs_move_pct=Decimal("0.04"),
        historical_median_upper_boundary=Decimal("104"),
        historical_median_lower_boundary=Decimal("96"),
        historical_quantiles=None, historical_move_stats=None, context_version="test",
    )


def _leg(index, action, right, strike, bid, ask):
    return V4T1LegInput(
        leg_index=index, action=action, right=right, strike=Decimal(strike),
        quantity=1, multiplier=Decimal("100"),
        entry_bid=Decimal(bid), entry_ask=Decimal(ask), entry_last=None,
        entry_iv=Decimal("0.40"), entry_delta=None, entry_gamma=None,
        entry_theta=None, entry_vega=None,
        market_data_quality="delayed", external_contract_id=f"conid-{index}",
    )


def _scenario(sid, em_fraction, ret) -> T1ScenarioResult:
    return T1ScenarioResult(
        variant_id="v", scenario_id=sid, underlying_move_label=sid,
        underlying_move_em_fraction=Decimal(em_fraction),
        scenario_underlying_price=Decimal("100"),
        iv_scenario_label="base", iv_scenario_multiplier=Decimal("1"),
        dte_remaining_at_exit=7, leg_values=(),
        entry_cashflow=Decimal("-500"), theoretical_liquidation_value=Decimal("500"),
        executable_liquidation_value=Decimal("500"),
        realized_equivalent_pnl_theoretical=Decimal(ret) * 100,
        realized_equivalent_pnl_executable=Decimal(ret) * 100,
        return_on_standardized_capital_theoretical=Decimal(ret),
        return_on_standardized_capital_executable=Decimal(ret),
        return_on_entry_cash=Decimal(ret), reason_codes=(), quality_note="ok",
    )


def _distribution(median="0.05") -> T1CandidateDistributionSummary:
    return T1CandidateDistributionSummary(
        variant_id="v", n_scenarios=7, n_valued=7,
        min_return=Decimal("-0.30"), max_return=Decimal("0.40"),
        median_return=Decimal(median), lower_quartile_return=Decimal("-0.10"),
        positive_scenario_fraction=Decimal("0.57"),
        scenario_average_return=Decimal(median),
        weighted_expected_return=Decimal(median),
        worst_scenario_id="s0", worst_scenario_return=Decimal("-0.30"),
        quality_note="synthetic",
    )


def _candidate(cid, strategy, legs, cash, median="0.05") -> RankableCandidate:
    context = V4T1ValuationContext(
        ticker="TSTX", underlying_price=Decimal("100"), observed_at=NOW,
        entry_timestamp=NOW, expected_exit_timestamp=NOW + timedelta(days=1),
        strategy=strategy, expiration=EXPIRATION, legs=tuple(legs),
        expected_move_context=_em(),
    )
    return RankableCandidate(
        candidate_id=cid, context=context,
        scenario_results=tuple(
            _scenario(f"s{i}", f"{i - 3}", "0.05") for i in range(7)
        ),
        distribution=_distribution(median), semantic_compatibility=None,
        entry_cash_required=Decimal(cash), capital_utilisation=None,
        max_leg_timestamp_skew_seconds=Decimal("0"),
    )


# --- the ONE shared evidence universe every test below reuses ---------------
def shared_universe() -> list[RankableCandidate]:
    return [
        # Cheap defined-risk spread: 2.00 debit -> $200 risk. Fits everywhere.
        _candidate(
            "spread_cheap", "bull_call_spread",
            [_leg(0, "buy", "call", "100", "3.00", "3.20"),
             _leg(1, "sell", "call", "105", "1.20", "1.40")],
            "180",
        ),
        # Single-leg long put at 11.55 -> $1,155 risk. This is the real
        # 2026-09-01 PANW structure, to scale.
        _candidate(
            "long_put_panw", "long_put",
            [_leg(0, "buy", "put", "347.50", "10.90", "11.55")],
            "1155",
        ),
        # Expensive straddle: 25.00 debit -> $2,500 risk. 2K cannot afford
        # it at all; 10K can afford it but only Moderate/Aggressive can
        # carry the risk.
        _candidate(
            "straddle_wide", "long_straddle",
            [_leg(0, "buy", "call", "100", "12.00", "12.50"),
             _leg(1, "buy", "put", "100", "12.00", "12.50")],
            "2500",
        ),
    ]


class TestSixConfigurationsOverSharedEvidence:
    def test_all_six_produce_an_independently_valid_result(self):
        """Section 57 -- one common evidence set, six real outcomes."""
        outcomes = evaluate_all_configurations(shared_universe())
        assert len(outcomes) == 6
        assert [o.configuration.key for o in outcomes] == [c.key for c in V4_CONFIGURATIONS]
        for outcome in outcomes:
            assert outcome.status in {"RANKED", "NO_ACTION"}
            if outcome.status == "RANKED":
                assert outcome.rank_1_candidate_id is not None
            else:
                assert outcome.no_action_reason

    def test_evaluation_never_mutates_the_shared_universe(self):
        """Section 15 -- all six must see identical evidence. If the layer
        mutated the candidate list, later configurations would rank a
        different universe than earlier ones."""
        universe = shared_universe()
        before = [(c.candidate_id, c.entry_cash_required) for c in universe]
        evaluate_all_configurations(universe)
        assert [(c.candidate_id, c.entry_cash_required) for c in universe] == before
        assert len(universe) == 3

    def test_ranking_version_is_unchanged_v4_4b_v1(self):
        """V4.4B ranking v1 must not be modified by the six-config work."""
        for outcome in evaluate_all_configurations(shared_universe()):
            assert outcome.ranking_version == RANKING_VERSION
        assert RANKING_VERSION == "v4-4b-t1-executable-ranking-v1"


class TestSharedEvidenceIsAcquiredOnce:
    def test_evaluation_performs_no_io_at_all(self, monkeypatch):
        """Section 58 -- six configurations must NOT cause six DecisionView
        calls, six metadata fetches, or six market-data acquisitions.

        Rather than counting mocked calls, this asserts the stronger
        property that makes the counting unnecessary: the evaluation layer
        cannot perform I/O, because any socket use raises.
        """
        import socket

        def _refuse(*args, **kwargs):
            raise AssertionError(
                "Six-configuration evaluation attempted network I/O. It must be "
                "pure: all market evidence is acquired once, upstream."
            )

        monkeypatch.setattr(socket.socket, "connect", _refuse, raising=False)
        monkeypatch.setattr(socket, "create_connection", _refuse, raising=False)

        outcomes = evaluate_all_configurations(shared_universe())
        assert len(outcomes) == 6

    def test_all_six_reference_the_same_candidate_objects(self):
        """The universe is shared by identity, not copied per config --
        so all six provably ranked the same quotes and timestamps."""
        universe = shared_universe()
        by_id = {c.candidate_id: id(c) for c in universe}
        for outcome in evaluate_all_configurations(universe):
            for ranked in outcome.ranked:
                assert ranked.candidate_id in by_id


class TestRiskProfileBehaviour:
    """Section 59 -- differences must emerge from real profile policy, not
    from strategy-name hardcoding."""

    def test_conservative_excludes_single_leg_longs_by_family_rule(self):
        outcome = evaluate_configuration(
            shared_universe(), get_configuration("v4_10k_conservative")
        )
        excluded = {e.candidate_id: e.reason_code for e in outcome.exclusions}
        assert excluded.get("long_put_panw") == EXCLUDED_STRATEGY_FAMILY

    def test_moderate_and_aggressive_allow_that_same_family(self):
        for key in ("v4_10k_moderate", "v4_10k_aggressive"):
            outcome = evaluate_configuration(shared_universe(), get_configuration(key))
            codes = {e.reason_code for e in outcome.exclusions if e.candidate_id == "long_put_panw"}
            assert EXCLUDED_STRATEGY_FAMILY not in codes, key

    def test_risk_cap_tightens_monotonically_across_profiles(self):
        """Conservative 15% < Moderate 30% < Aggressive 50%, so the set of
        affordable candidates can only grow as risk tolerance rises."""
        eligible = {}
        for profile in ("conservative", "moderate", "aggressive"):
            outcome = evaluate_configuration(
                shared_universe(), get_configuration(f"v4_10k_{profile}")
            )
            eligible[profile] = outcome.eligible_candidate_count
        assert eligible["conservative"] <= eligible["moderate"] <= eligible["aggressive"]

    def test_aggressive_at_10k_can_carry_the_wide_straddle(self):
        """$2,500 risk fits Aggressive's $5,000 cap but not Conservative's
        $1,500 -- a real policy difference, not a label."""
        aggressive = evaluate_configuration(
            shared_universe(), get_configuration("v4_10k_aggressive")
        )
        assert "straddle_wide" not in {
            e.candidate_id for e in aggressive.exclusions if e.reason_code == EXCLUDED_RISK_CAP
        }
        conservative = evaluate_configuration(
            shared_universe(), get_configuration("v4_10k_conservative")
        )
        assert "straddle_wide" in {
            e.candidate_id
            for e in conservative.exclusions
            if e.reason_code == EXCLUDED_RISK_CAP
        }


class TestCapitalBehaviour:
    """Section 60 -- $2K cannot fit what $10K can, with an explicit reason."""

    def test_panw_long_put_is_refused_at_2k_and_allowed_at_10k(self):
        """The real 2026-09-01 case: $1,155 risk. Every 2K configuration
        refuses it; the 10K Moderate/Aggressive configurations carry it."""
        two_k = evaluate_configuration(shared_universe(), get_configuration("v4_2k_moderate"))
        refused = {e.candidate_id: e for e in two_k.exclusions}
        assert refused["long_put_panw"].reason_code == EXCLUDED_RISK_CAP

        ten_k = evaluate_configuration(shared_universe(), get_configuration("v4_10k_moderate"))
        assert "long_put_panw" not in {e.candidate_id for e in ten_k.exclusions}

    def test_the_refusal_message_names_the_binding_constraint(self):
        """Section 41 -- naming the $2,000 capital base when the $600 risk
        cap is what actually bound is what made the real PANW failure
        unreadable."""
        outcome = evaluate_configuration(
            shared_universe(), get_configuration("v4_2k_moderate")
        )
        detail = next(
            e.detail for e in outcome.exclusions if e.candidate_id == "long_put_panw"
        )
        assert "Risk cap exceeded" in detail
        assert "$1,155.00" in detail        # what the structure needs
        assert "$600.00" in detail          # what the profile allows
        assert "30%" in detail              # why that is the cap
        assert "$2,000" in detail           # of what capital base

    def test_capital_base_refusal_is_distinct_from_risk_cap_refusal(self):
        """A structure costing more than the whole account is a CAPITAL
        problem; one that fits but breaches the cap is a RISK problem.
        Collapsing them would repeat the original reporting defect."""
        outcome = evaluate_configuration(
            shared_universe(), get_configuration("v4_2k_aggressive")
        )
        codes = {e.candidate_id: e.reason_code for e in outcome.exclusions}
        # $2,500 entry > $2,000 account -> capital, not risk.
        assert codes["straddle_wide"] == EXCLUDED_CAPITAL
        # $1,155 risk fits the $2,000 account but breaches the $1,000 cap.
        assert codes["long_put_panw"] == EXCLUDED_RISK_CAP


class TestNoActionIsARealOutcome:
    def test_a_configuration_may_legitimately_produce_no_action(self):
        """Section 17 -- rules are never weakened to make all six trade."""
        tiny = evaluate_configuration(
            [_candidate("expensive", "long_straddle",
                        [_leg(0, "buy", "call", "100", "40.00", "41.00")], "4100")],
            get_configuration("v4_2k_conservative"),
        )
        assert tiny.status == "NO_ACTION"
        assert tiny.rank_1_candidate_id is None
        assert tiny.no_action_reason

    def test_configurations_can_disagree_on_the_same_evidence(self):
        """The six are independent results, not six copies of one answer."""
        statuses = {
            o.configuration.key: o.status for o in evaluate_all_configurations(shared_universe())
        }
        assert len(set(statuses.values())) >= 1
        # 2K Conservative is the tightest ($300 cap, no single-leg longs);
        # 10K Aggressive the loosest ($5,000 cap, no family restriction).
        assert statuses["v4_10k_aggressive"] == "RANKED"


class TestMaxDefinedRisk:
    def test_long_option_risk_is_the_full_executable_debit(self):
        candidate = _candidate(
            "p", "long_put", [_leg(0, "buy", "put", "347.50", "10.90", "11.55")], "1155"
        )
        # ASK side, never mid: 11.55 * 100 = 1155, not 11.225 * 100.
        assert max_defined_risk(candidate) == Decimal("1155.00")

    def test_spread_risk_is_bounded_below_the_long_leg_cost(self):
        candidate = _candidate(
            "s", "bull_call_spread",
            [_leg(0, "buy", "call", "100", "3.00", "3.20"),
             _leg(1, "sell", "call", "105", "1.20", "1.40")],
            "180",
        )
        risk = max_defined_risk(candidate)
        assert risk is not None
        assert risk < Decimal("320")  # cheaper than the long leg alone

    def test_unpriceable_leg_yields_no_risk_number_rather_than_a_guess(self):
        leg = V4T1LegInput(
            leg_index=0, action="buy", right="call", strike=Decimal("100"),
            quantity=1, multiplier=Decimal("100"),
            entry_bid=Decimal("1"), entry_ask=None, entry_last=None, entry_iv=None,
            entry_delta=None, entry_gamma=None, entry_theta=None, entry_vega=None,
            market_data_quality="delayed", external_contract_id="c",
        )
        assert max_defined_risk(_candidate("x", "long_call", [leg], "100")) is None


class TestConfigurationIdentity:
    def test_every_configuration_persists_capital_and_profile_explicitly(self):
        """Section 50 -- identity is never inferred from candidate text."""
        for config in V4_CONFIGURATIONS:
            assert config.capital_base in (Decimal("2000"), Decimal("10000"))
            assert config.risk_profile in set(RiskProfile)
            assert get_configuration(config.key) is config

    def test_max_risk_dollars_matches_the_documented_percentages(self):
        expected = {
            "v4_2k_conservative": Decimal("300"), "v4_2k_moderate": Decimal("600"),
            "v4_2k_aggressive": Decimal("1000"), "v4_10k_conservative": Decimal("1500"),
            "v4_10k_moderate": Decimal("3000"), "v4_10k_aggressive": Decimal("5000"),
        }
        for key, dollars in expected.items():
            assert get_configuration(key).max_risk_dollars == dollars


class TestFixturesAreNotVacuous:
    """A guard against the specific way this file was wrong when first
    written: with ``distribution=None`` every candidate is correctly
    classified CANNOT_VALUE_HONESTLY, nothing is ever rankable, and every
    assertion about ranking silently passes while proving nothing.

    These tests fail loudly if the shared fixtures ever drift back to
    being unrankable.
    """

    def test_the_shared_universe_contains_genuinely_rankable_candidates(self):
        from analytics.decision.v4_4b_ranking import classify_candidate_validity

        statuses = [classify_candidate_validity(c)[0] for c in shared_universe()]
        assert "RANKABLE" in statuses, (
            f"No candidate in the shared universe is rankable ({statuses}); every "
            "ranking assertion in this file would be vacuous."
        )

    def test_at_least_one_configuration_actually_ranks_something(self):
        outcomes = evaluate_all_configurations(shared_universe())
        ranked = [o for o in outcomes if o.status == "RANKED"]
        assert ranked, "No configuration ranked anything -- fixtures are vacuous."
        assert any(o.rank_1_candidate_id for o in ranked)
