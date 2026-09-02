"""V4.4C -- structural isolation, no-look-ahead, and activation-gate
guarantees (Sections 44, 45, 48, 50).

These are the tests that make "V4 cannot affect V3" and "V4 cannot see
the future" checkable facts rather than intentions. Each one fails loudly
the moment the property stops holding, which is precisely when a human
should be asked instead of discovering it after official evidence has
been written.
"""

import ast
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parents[1] / "src"

#: Modules that produce OFFICIAL V3 evidence.
OFFICIAL_PATH_MODULES = (
    "services/decision_engine.py",
    "services/decision_snapshot_freezing.py",
    "services/benchmark_entry_capture.py",
    "services/benchmark_exit_capture.py",
    "services/options_reconstruction.py",
)

SHADOW_MODULES = (
    "services/v4_shadow.py",
    "analytics/decision/v4_t1_stress_grid.py",
    "analytics/decision/v4_4b_ranking.py",
)

#: Anything that carries a realized outcome. Shadow DECISION-time code
#: must not be able to reach any of it.
OUTCOME_MODULES = (
    "settlement_snapshot",
    "settlement_capture_attempt",
    "exit_snapshot",
    "price_reaction",
    "earnings_result",
    "benchmark_exit_capture",
)


def _imports(path: Path) -> set[str]:
    """Real import extraction via AST -- not a substring grep, which
    would be fooled by the word appearing in a comment or docstring."""
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{a.name}" for a in node.names)
    return found


class TestNoLookAhead:
    @pytest.mark.parametrize("module_path", SHADOW_MODULES)
    def test_shadow_decision_code_cannot_import_realized_outcomes(self, module_path):
        """Section 44/63 -- the decision-time path must be structurally
        incapable of reading an outcome for the event being decided."""
        imported = _imports(_SRC / module_path)
        leaked = [m for m in imported for o in OUTCOME_MODULES if o in m]
        assert not leaked, f"{module_path} can reach realized-outcome data: {leaked}"

    @pytest.mark.parametrize("module_path", SHADOW_MODULES)
    def test_shadow_code_references_no_outcome_field_names(self, module_path):
        source = (_SRC / module_path).read_text()
        # V4ShadowSettlement is the ONE place outcome lives, and the
        # shadow service legitimately imports the model module -- but it
        # must not reference outcome VALUES during decision generation.
        for banned in ("realized_pnl =", "actual_return", "outcome_label", "settled_return"):
            assert banned not in source, f"{module_path} references outcome field {banned!r}"

    def test_outcome_lives_only_on_the_settlement_table(self):
        """Section 63 -- label leakage is prevented by schema shape, not
        by discipline: the frozen decision has no column that could hold
        a realized result."""
        from models.v4_shadow import V4ShadowCandidate, V4ShadowDecision, V4ShadowSettlement

        # Token match, not substring: "legal_decision_window_at" legitimately
        # contains "win", and a naive substring check flags it as an outcome
        # column. Splitting on "_" compares whole name parts instead.
        outcome_tokens = {"realized", "pnl", "outcome", "settled", "win", "loss"}
        for model in (V4ShadowDecision, V4ShadowCandidate):
            for column in model.__table__.columns:
                tokens = set(column.name.split("_"))
                assert not (tokens & outcome_tokens), (
                    f"{model.__tablename__}.{column.name} looks like an outcome column -- "
                    "outcome must live only on v4_shadow_settlement"
                )
        settlement_columns = {c.name for c in V4ShadowSettlement.__table__.columns}
        assert "realized_pnl" in settlement_columns


class TestV3Isolation:
    @pytest.mark.parametrize("module_path", OFFICIAL_PATH_MODULES)
    def test_official_module_does_not_import_shadow_code(self, module_path):
        """A V4 failure must never be able to break V3 -- easiest way to
        guarantee that is for V3 not to import V4 at all."""
        imported = _imports(_SRC / module_path)
        leaked = [m for m in imported if "v4_shadow" in m or "v4_4b_ranking" in m]
        assert not leaked, f"{module_path} imports V4 shadow code: {leaked}"

    def test_shadow_service_never_writes_official_v3_tables(self):
        """Section 9 -- shadow evidence lives in its own tables."""
        imported = _imports(_SRC / "services/v4_shadow.py")
        official_models = (
            "models.decision_snapshot",
            "models.entry_snapshot",
            "models.exit_snapshot",
            "models.settlement_snapshot",
            "models.entry_capture_attempt",
            "models.settlement_capture_attempt",
        )
        leaked = [m for m in imported for o in official_models if m.startswith(o)]
        assert not leaked, f"shadow service reaches official V3 evidence models: {leaked}"

    def test_shadow_service_has_no_brokerage_order_surface(self):
        """Section 10 -- shadow means analytical observation only."""
        source = (_SRC / "services/v4_shadow.py").read_text()
        for banned in ("placeOrder", "cancelOrder", "exerciseOptions", "reqGlobalCancel"):
            assert banned not in source

    def test_observation_is_never_called_a_fill(self):
        """Naming matters: an observed quote is not an execution."""
        from models.v4_shadow import V4ShadowObservation

        columns = {c.name for c in V4ShadowObservation.__table__.columns}
        for banned in ("fill", "fill_price", "order_id", "position"):
            assert banned not in columns


class TestActivationGate:
    def test_shadow_is_disabled_by_default(self):
        """Sections 48/50 -- must default OFF, and stay OFF until
        activation is explicitly authorized."""
        from core.config import Settings

        assert Settings(_env_file=None).v4_shadow_enabled is False

    def test_activation_is_an_explicit_environment_decision_never_a_code_default(self):
        """The shadow cohort was activated in production on 2026-09-02 after
        the live activation gate (dry-run PASS_WITH_WARNINGS, zero writes).
        Activation is carried ONLY by the environment: the code default
        stays False, so a fresh deployment never starts the cohort
        implicitly, and the running value is always an explicit bool."""
        from core.config import Settings, get_settings

        assert Settings(_env_file=None).v4_shadow_enabled is False
        assert isinstance(get_settings().v4_shadow_enabled, bool)


class TestImmutabilityShape:
    def test_every_shadow_table_is_append_only_by_design(self):
        """The DB-level trigger is installed by the migration; this
        asserts the models carry no updated_at, which would imply
        mutation is expected."""
        from models.v4_shadow import (
            V4ShadowCandidate,
            V4ShadowCandidateLeg,
            V4ShadowDecision,
            V4ShadowObservation,
            V4ShadowRunEvent,
            V4ShadowSettlement,
        )

        for model in (
            V4ShadowDecision,
            V4ShadowCandidate,
            V4ShadowCandidateLeg,
            V4ShadowObservation,
            V4ShadowSettlement,
            V4ShadowRunEvent,
        ):
            columns = {c.name for c in model.__table__.columns}
            assert "updated_at" not in columns, (
                f"{model.__tablename__} has updated_at -- shadow evidence is append-only"
            )

    def test_idempotency_constraint_exists_on_shadow_decisions(self):
        """Section 47 -- a scheduler retry must not create a duplicate
        shadow decision for the same event/window/engine."""
        from models.v4_shadow import V4ShadowDecision

        names = {c.name for c in V4ShadowDecision.__table__.constraints if c.name}
        assert "uq_v4_shadow_decision_event_window_engine" in names

    def test_same_event_identity_is_a_real_foreign_key(self):
        """Section 46 -- comparison must key on the authoritative event
        id, never a ticker/date match."""
        from models.v4_shadow import V4ShadowDecision

        fks = {
            fk.column.table.name
            for c in V4ShadowDecision.__table__.columns
            for fk in c.foreign_keys
        }
        assert "earnings_calendar_event" in fks


class TestStressGridSeparation:
    def test_core_grid_is_untouched_by_the_stress_expansion(self):
        """Sections 16/17 -- V4.4A's core grid must remain exactly seven
        points at +/-1.0 EM, or V4.4B's frozen ranking silently changes
        meaning."""
        from analytics.decision.v4_t1_scenario_grid import _UNDERLYING_MOVE_GRID

        assert len(_UNDERLYING_MOVE_GRID) == 7
        fractions = [f for _, f in _UNDERLYING_MOVE_GRID]
        assert max(fractions) == 1 and min(fractions) == -1

    def test_stress_points_lie_strictly_outside_the_core_envelope(self):
        from decimal import Decimal

        from analytics.decision.v4_t1_stress_grid import STRESS_EM_FRACTIONS

        for _label, fraction in STRESS_EM_FRACTIONS:
            assert abs(fraction) > Decimal(1)

    def test_stress_grid_imports_no_outcome_data(self):
        """Section 15 -- the expansion is justified structurally, and the
        module has no way to have been fitted to losses."""
        imported = _imports(_SRC / "analytics/decision/v4_t1_stress_grid.py")
        leaked = [m for m in imported for o in OUTCOME_MODULES if o in m]
        assert not leaked

    def test_ranking_version_still_frozen_at_v1(self):
        """Section 2 -- V4.4C must not change V4.4B's ranking."""
        from analytics.decision.v4_4b_ranking import RANKING_VERSION

        assert RANKING_VERSION == "v4-4b-t1-executable-ranking-v1"
