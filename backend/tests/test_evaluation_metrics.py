import pytest

from evaluation.metrics import (
    citation_completeness,
    citation_precision,
    fact_coverage,
    mean_reciprocal_rank,
    recall_at_k,
)


class TestRecallAtK:
    def test_all_relevant_within_k(self):
        assert recall_at_k([10, 20, 30], {10, 20}, k=3) == 1.0

    def test_partial_recall(self):
        assert recall_at_k([10, 99, 30], {10, 20}, k=3) == 0.5

    def test_relevant_outside_k_not_counted(self):
        assert recall_at_k([10, 99, 88, 20], {10, 20}, k=2) == 0.5

    def test_no_overlap(self):
        assert recall_at_k([1, 2, 3], {99}, k=3) == 0.0

    def test_empty_relevant_raises(self):
        with pytest.raises(ValueError, match="relevant_ids"):
            recall_at_k([1, 2, 3], set(), k=3)


class TestMeanReciprocalRank:
    def test_first_rank_hit(self):
        assert mean_reciprocal_rank([10, 20], {10}) == 1.0

    def test_third_rank_hit(self):
        assert mean_reciprocal_rank([1, 2, 10], {10}) == pytest.approx(1 / 3)

    def test_no_hit(self):
        assert mean_reciprocal_rank([1, 2, 3], {99}) == 0.0

    def test_empty_retrieved(self):
        assert mean_reciprocal_rank([], {99}) == 0.0


class TestCitationPrecision:
    def test_all_cited_relevant(self):
        assert citation_precision([1, 2], {1, 2, 3}) == 1.0

    def test_some_cited_irrelevant(self):
        assert citation_precision([1, 99], {1, 2, 3}) == 0.5

    def test_nothing_cited_scores_zero_not_perfect(self):
        assert citation_precision([], {1, 2, 3}) == 0.0


class TestCitationCompleteness:
    def test_all_relevant_cited(self):
        assert citation_completeness([1, 2, 3, 4], {1, 2}) == 1.0

    def test_half_relevant_cited(self):
        assert citation_completeness([1], {1, 2}) == 0.5

    def test_empty_relevant_raises(self):
        with pytest.raises(ValueError, match="relevant_ids"):
            citation_completeness([1], set())


class TestFactCoverage:
    def test_all_facts_present_case_insensitive(self):
        coverage, missing = fact_coverage(
            "AMD's Data Center revenue grew to $16.6 billion.", ["$16.6 billion", "data center"]
        )
        assert coverage == 1.0
        assert missing == []

    def test_missing_fact_reported(self):
        coverage, missing = fact_coverage(
            "Revenue grew nicely this year.", ["$16.6 billion", "32%"]
        )
        assert coverage == 0.0
        assert missing == ["$16.6 billion", "32%"]

    def test_partial_coverage(self):
        coverage, missing = fact_coverage("Revenue grew 32%.", ["32%", "$16.6 billion"])
        assert coverage == 0.5
        assert missing == ["$16.6 billion"]

    def test_no_required_facts_is_trivially_covered(self):
        coverage, missing = fact_coverage("anything", [])
        assert coverage == 1.0
        assert missing == []
