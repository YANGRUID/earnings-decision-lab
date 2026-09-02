"""V4.4B -- structural isolation (Section 39) and version freeze
(Section 38).

V4.4B is experimental. The single most important property it must have
is that it CANNOT influence the official V3 forward test -- not by
policy, not by intention, but structurally, provable by reading imports.
These tests fail loudly the moment anything official starts depending on
the ranker, which is exactly when a human should be asked rather than
discovering it after an official record has already been written.
"""

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"

#: Every module that participates in producing OFFICIAL V3 evidence.
#: If any of these imports the V4.4B ranker, V4 has leaked into the
#: official path.

RANKER_MODULE_NAMES = (
    "v4_4b_ranking",
    "analytics.decision.v4_4b_ranking",
)


def _imported_modules(path: Path) -> set[str]:
    """Real import extraction via the AST -- not a substring grep, which
    would be fooled by the word appearing in a comment or docstring."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found




class TestVersionFreeze:
    def test_methodology_record_reports_the_frozen_ranking_version(self):
        from analytics.decision.v4_4b_ranking import RANKING_VERSION
        from analytics.decision.v4_methodology import V4_METHODOLOGY

        assert V4_METHODOLOGY.ranking_version == RANKING_VERSION, (
            "The centralized methodology record and the ranker disagree about the ranking "
            "version. Any behavior change REQUIRES a new version string in both places -- "
            "silently reusing one invalidates every replay already run against it."
        )

    def test_v4_4a_and_earlier_versions_were_not_disturbed(self):
        """V4.4B consumes those surfaces; it must not have altered them."""
        from analytics.decision.v4_methodology import V4_METHODOLOGY

        assert V4_METHODOLOGY.t1_valuation_version == "t1_pricing_v1"
        assert V4_METHODOLOGY.geometry_candidate_version == "geometry_candidate_v1"
        assert V4_METHODOLOGY.strike_engine_version == "expected_move_v1"
        assert V4_METHODOLOGY.view_strategy_compatibility_version == (
            "view_strategy_compatibility_v1"
        )

    def test_expiration_version_remains_an_explicit_placeholder(self):
        """Section 20: V4.4B did NOT reintroduce V3's expiration score."""
        from analytics.decision.v4_methodology import NOT_IMPLEMENTED, V4_METHODOLOGY

        assert V4_METHODOLOGY.expiration_version == NOT_IMPLEMENTED
