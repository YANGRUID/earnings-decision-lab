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

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"

#: Every module that participates in producing OFFICIAL V3 evidence.
#: If any of these imports the V4.4B ranker, V4 has leaked into the
#: official path.
OFFICIAL_PATH_MODULES = (
    "services/scheduler.py",
    "services/decision_engine.py",
    "services/decision_snapshot_freezing.py",
    "services/benchmark_entry_capture.py",
    "services/benchmark_exit_capture.py",
    "services/options_reconstruction.py",
)

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


class TestOfficialPathIsolation:
    @pytest.mark.parametrize("module_path", OFFICIAL_PATH_MODULES)
    def test_official_module_does_not_import_the_v4_4b_ranker(self, module_path):
        path = _SRC / module_path
        assert path.exists(), f"{module_path} moved -- update this isolation test deliberately"
        imported = _imported_modules(path)
        leaked = [m for m in imported if any(name in m for name in RANKER_MODULE_NAMES)]
        assert not leaked, (
            f"{module_path} imports the experimental V4.4B ranker ({leaked}). V4.4B must "
            "never participate in official decision, entry, or settlement evidence."
        )

    def test_no_official_module_anywhere_imports_the_ranker(self):
        """Broader sweep than the explicit list above, so a newly-added
        official module is covered without anyone remembering to list
        it. Only the ranker's own tests, the experimental router, and
        replay tooling may import it."""
        # v4_shadow.py (V4.4C) is V4 shadow-generation code -- consuming
        # the ranker is its entire purpose. The guarantee that matters,
        # that no OFFICIAL V3 module imports the ranker, is asserted by
        # test_official_module_does_not_import_the_v4_4b_ranker above
        # and is unaffected by this entry.
        allowed_substrings = (
            "v4_4b_ranking.py",
            "v4_experimental.py",
            "v4_4b_replay.py",
            "services/v4_shadow.py",
            "api/routers/v4_shadow.py",
            "services/v4_shadow_assembler.py",
            "services/v4_shadow_settlement.py",
            # V4 six-configuration layer (2026-09-02). Sits ABOVE the
            # ranker -- it filters the shared candidate universe per
            # capital/risk configuration and then calls the unchanged
            # rank_candidates. V4-only; not in OFFICIAL_PATH_MODULES, so
            # the invariant that matters is untouched.
            "services/v4_config_evaluation.py",
            # Read-only V4 dry-run, gated behind ENABLE_INTERNAL_DIAGNOSTICS.
            "api/routers/tws_diagnostics.py",
        )
        offenders = []
        for path in _SRC.rglob("*.py"):
            if any(a in str(path) for a in allowed_substrings):
                continue
            try:
                imported = _imported_modules(path)
            except SyntaxError:  # pragma: no cover -- defensive
                continue
            if any(name in m for m in imported for name in RANKER_MODULE_NAMES):
                offenders.append(str(path.relative_to(_SRC)))
        assert not offenders, f"unexpected importers of the V4.4B ranker: {offenders}"

    def test_ranker_does_not_import_official_capture_or_persistence(self):
        """The other direction: the ranker must be a pure analytics
        module. If it can reach entry capture or a DB session, it could
        write official evidence -- so it must not be able to."""
        imported = _imported_modules(_SRC / "analytics/decision/v4_4b_ranking.py")
        forbidden = (
            "db.session",
            "sqlalchemy",
            "benchmark_entry_capture",
            "benchmark_exit_capture",
            "decision_snapshot_freezing",
            "models.decision_snapshot",
            "services.scheduler",
        )
        leaked = [m for m in imported for f in forbidden if f in m]
        assert not leaked, f"ranker reaches persistence/official capture: {leaked}"

    def test_ranker_has_no_realized_outcome_input(self):
        """Section 1/28: no realized outcome may reach the ranking
        methodology at all -- not as an argument, not as an import."""
        imported = _imported_modules(_SRC / "analytics/decision/v4_4b_ranking.py")
        outcome_modules = ("settlement", "exit_snapshot", "price_reaction", "earnings_result")
        leaked = [m for m in imported for o in outcome_modules if o in m]
        assert not leaked, f"ranker imports realized-outcome data: {leaked}"

        source = (_SRC / "analytics/decision/v4_4b_ranking.py").read_text()
        for banned in ("realized_pnl", "actual_return", "settled_", "outcome_label"):
            assert banned not in source, f"ranker references realized outcome field {banned!r}"


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
