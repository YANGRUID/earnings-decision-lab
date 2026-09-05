"""V4.2 -- per-configuration outcomes, and the earnings-friction cohort.

Two independent Phase-1 foundations, both deliberately conservative: the six
configurations may now disagree on the same evidence, and the friction cohort
refuses to become a model until it holds evidence comparable to the one it
would replace.
"""

from datetime import date
from decimal import Decimal

from analytics.decision.v4_2_earnings_friction import (
    MIN_EVENTS_FOR_MODEL,
    MIN_OBSERVATIONS_FOR_MODEL,
    STATUS_ADVISORY,
    STATUS_READY,
    FrictionObservation,
    build_earnings_friction_cohort,
)
from analytics.decision.v4_2_viability import (
    CAPITAL_INCOMPATIBLE,
    RISK_CAP_EXCEEDED,
    CandidateEconomics,
    ConfigurationConstraints,
    MoveEvidence,
    choose_v4_2_candidate_for_configuration,
)
from analytics.earnings.v4_2_move_distribution import build_move_distribution

D = Decimal

CONSERVATIVE_2K = ConfigurationConstraints(
    key="v4_2k_conservative", capital_base=D("2000"), max_risk_dollars=D("200")
)
MODERATE_10K = ConfigurationConstraints(
    key="v4_10k_moderate", capital_base=D("10000"), max_risk_dollars=D("2000")
)

EVIDENCE = MoveEvidence(
    implied_move_pct=D("0.10"),
    distribution=build_move_distribution(
        [D("0.05") if i % 2 else D("-0.05") for i in range(20)], as_of=date(2026, 9, 3)
    ),
)


def _sound(cid="viable", strategy="bull_call_spread"):
    return CandidateEconomics(
        candidate_id=cid, strategy=strategy, median_return=D("0.06"),
        worst_return=D("-0.10"), best_return=D("0.30"),
        positive_scenario_fraction=D("0.50"), no_profitable_region=False,
        semantic_compatibility=D("1.0"), mean_relative_spread=D("0.05"),
    )


class TestConfigurationsMayDisagree:
    def test_the_same_evidence_can_action_one_configuration_and_not_another(self):
        """Sections 41-42: the six are not slots to be filled."""
        candidates = [_sound()]
        cash = {"viable": D("4000")}
        small = choose_v4_2_candidate_for_configuration(
            candidates, EVIDENCE, CONSERVATIVE_2K, entry_cash_by_candidate=cash
        )
        large = choose_v4_2_candidate_for_configuration(
            candidates, EVIDENCE, MODERATE_10K, entry_cash_by_candidate=cash
        )
        assert small.status == "NO_ACTION"
        assert CAPITAL_INCOMPATIBLE in (small.no_action_reason or "")
        assert large.status == "RANKED"

    def test_a_defined_risk_above_the_cohorts_cap_is_refused_for_that_cohort(self):
        out = choose_v4_2_candidate_for_configuration(
            [_sound()], EVIDENCE, CONSERVATIVE_2K,
            max_loss_by_candidate={"viable": D("900")},
        )
        assert out.status == "NO_ACTION"
        assert RISK_CAP_EXCEEDED in (out.no_action_reason or "")

    def test_a_candidate_within_both_limits_is_actioned(self):
        out = choose_v4_2_candidate_for_configuration(
            [_sound()], EVIDENCE, CONSERVATIVE_2K,
            entry_cash_by_candidate={"viable": D("150")},
            max_loss_by_candidate={"viable": D("150")},
        )
        assert out.status == "RANKED"
        assert out.selected_candidate_id == "viable"

    def test_the_economic_gate_is_identical_across_configurations(self):
        """An economically bad trade is bad at every size -- only capital and
        risk fit differ per cohort."""
        bad = CandidateEconomics(
            candidate_id="bad", strategy="bull_call_spread", median_return=D("-0.05"),
            worst_return=D("-0.10"), best_return=D("0.10"),
            positive_scenario_fraction=D("0.30"), no_profitable_region=False,
            semantic_compatibility=D("1.0"), mean_relative_spread=D("0.05"),
        )
        for constraints in (CONSERVATIVE_2K, MODERATE_10K):
            out = choose_v4_2_candidate_for_configuration([bad], EVIDENCE, constraints)
            assert out.status == "NO_ACTION"

    def test_unknown_capital_and_risk_do_not_silently_pass_as_zero(self):
        """With no cash or loss figure supplied the capital gate cannot fire;
        the candidate is admitted on economics alone rather than being
        credited with a fabricated zero requirement."""
        out = choose_v4_2_candidate_for_configuration([_sound()], EVIDENCE, CONSERVATIVE_2K)
        assert out.status == "RANKED"


class TestEarningsFrictionCohort:
    def _obs(self, spread: str, dte: int = 1):
        return FrictionObservation(
            relative_spread=D(spread), absolute_spread=D("0.05"), dte=dte, moneyness=D("1.0")
        )

    def test_a_small_cohort_stays_advisory_and_proposes_nothing(self):
        cohort = build_earnings_friction_cohort(
            [self._obs("0.10") for _ in range(210)], distinct_events=7
        )
        assert cohort.status == STATUS_ADVISORY
        assert not cohort.ready
        assert cohort.proposed_friction_levels() is None

    def test_enough_observations_across_enough_events_becomes_reviewable(self):
        cohort = build_earnings_friction_cohort(
            [self._obs("0.10") for _ in range(MIN_OBSERVATIONS_FOR_MODEL)],
            distinct_events=MIN_EVENTS_FOR_MODEL,
        )
        assert cohort.status == STATUS_READY
        assert cohort.proposed_friction_levels() is not None

    def test_many_observations_from_too_few_events_is_still_advisory(self):
        """Seven events cannot become a distribution by quoting more legs
        from each of them."""
        cohort = build_earnings_friction_cohort(
            [self._obs("0.10") for _ in range(MIN_OBSERVATIONS_FOR_MODEL)], distinct_events=7
        )
        assert cohort.status == STATUS_ADVISORY

    def test_the_quantiles_use_the_same_construction_as_the_incumbent(self):
        cohort = build_earnings_friction_cohort(
            [self._obs(s) for s in ("0.04", "0.08", "0.12", "0.16")], distinct_events=4
        )
        assert cohort.p25_relative_spread == D("0.07")
        assert cohort.p50_relative_spread == D("0.10")
        assert cohort.p75_relative_spread == D("0.13")

    def test_the_tail_the_incumbent_cannot_express_is_recorded(self):
        cohort = build_earnings_friction_cohort(
            [self._obs(f"0.{i:02d}") for i in range(1, 21)], distinct_events=5
        )
        assert cohort.p90_relative_spread is not None
        assert cohort.max_relative_spread == D("0.20")

    def test_an_empty_cohort_reports_zero_rather_than_failing(self):
        cohort = build_earnings_friction_cohort([], distinct_events=0)
        assert cohort.observations == 0
        assert cohort.proposed_friction_levels() is None

    def test_the_cohort_carries_its_own_version(self):
        cohort = build_earnings_friction_cohort([], distinct_events=0)
        assert cohort.version == "earnings_friction_v2"
