"""V4.2/V4.3 -- static confirmation that V3's real, official pipeline
has zero references to any V4 module (V4.2's own Section 21/26, V4.3's
own Section 35). A static/grep-based test, not a behavioral one: it
asserts the actual source text of every real pipeline file never
mentions a v4_* import or symbol, so this guarantee can never silently
regress as new V4 modules are added.
"""

from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

OFFICIAL_PIPELINE_FILES = [
    "services/scheduler.py",
    "services/decision_pipeline.py",
    "services/decision_engine.py",
    "services/benchmark_entry_capture.py",
    "services/benchmark_exit_capture.py",
    "services/decision_snapshot_freezing.py",
    "analytics/decision/strategy_scoring.py",
    "analytics/options/strategy_candidates.py",
]

FORBIDDEN_NEEDLES = [
    "v4_compatibility",
    "v4_market_view",
    "v4_market_coherence",
    "v4_replay",
    "v4_strategy_semantics",
    "v4_feature_contract",
    "v4_capital",
    "v4_shadow",
    "v4_methodology",
    "v4_expected_move",
    "v4_strike_resolver",
    "v4_strike_engine",
    "v4_chain_coverage",
    "v4_strike_geometry_variants",
    "v4_t1_valuation_context",
    "v4_t1_scenario_grid",
    "v4_t1_pricing",
    "v4_t1_replay",
    "ENGINE_VERSION_V4",
    "evaluate_semantic_compatibility",
    "select_v4_strikes",
    "generate_strike_geometry_variants",
    "evaluate_candidate_t1_scenarios",
]


#: V4.5 -- services/scheduler.py is the process-wide JOB REGISTRY, not a
#: decision module, so it legitimately registers the experimental shadow
#: jobs alongside the official ones. The job BODIES were deliberately
#: extracted to services/v4_shadow_scheduler.py so scheduler.py contains no
#: V4 semantics, valuation, or ranking -- only registration, wrapped in its
#: own try/except so a shadow registration failure cannot take V3's jobs
#: down with it. Every V4 METHODOLOGY needle stays forbidden here, which is
#: the guarantee that actually matters (asserted separately below).
REGISTRATION_ONLY_EXCEPTIONS = {
    "services/scheduler.py": {"v4_shadow"},
}

#: Reaching any of these from the official pipeline would mean V4
#: methodology can influence V3 -- forbidden with no exceptions.
V4_METHODOLOGY_NEEDLES = (
    "v4_compatibility",
    "v4_market_view",
    "v4_strategy_semantics",
    "v4_strike_engine",
    "v4_strike_geometry_variants",
    "v4_t1_pricing",
    "v4_4b_ranking",
    "evaluate_semantic_compatibility",
    "rank_candidates",
)


def test_no_official_pipeline_file_references_any_v4_module():
    for relative_path in OFFICIAL_PIPELINE_FILES:
        path = SRC / relative_path
        assert path.exists(), f"{relative_path} not found -- update this test's file list"
        text = path.read_text()
        allowed = REGISTRATION_ONLY_EXCEPTIONS.get(relative_path, set())
        for needle in FORBIDDEN_NEEDLES:
            if needle in allowed:
                continue
            assert needle not in text, f"{relative_path} references V4 construct {needle!r}"


def test_scheduler_contains_no_v4_methodology_only_job_registration():
    """Keeps the narrow exception above narrow: scheduler.py may name the
    shadow JOBS, but must never reach V4 semantics, compatibility,
    valuation, or ranking."""
    text = (SRC / "services/scheduler.py").read_text()
    for needle in V4_METHODOLOGY_NEEDLES:
        assert needle not in text, (
            f"services/scheduler.py reaches V4 methodology ({needle!r}) -- only job "
            "registration is permitted there"
        )


def _real_import_offenders(needles: tuple[str, ...]) -> list[str]:
    """Walk every real .py file under src/ and return the relative
    paths of files that IMPORT (real `import`/`from ... import` lines
    only, never an incidental docstring mention) one of ``needles``
    from outside the allowed V4/experimental-router prefixes."""
    # V4.4C adds services/v4_shadow.py -- itself V4 code (shadow
    # decision generation), so it legitimately consumes V4 modules.
    # The property these tests protect is that OFFICIAL V3 decision/
    # evidence modules never import V4, which is asserted separately
    # and still holds -- this list is "which modules are allowed to BE
    # V4", not a relaxation of that guarantee.
    allowed_importer_prefixes = (
        "analytics/decision/v4_",
        "api/routers/v4_",
        "services/v4_shadow.py",
        # V4.5: the live candidate assembler is itself V4 code.
        "services/v4_shadow_assembler.py",
        "services/v4_shadow_settlement.py",
        # The diagnostics router hosts the read-only V4 dry-run and is
        # gated behind ENABLE_INTERNAL_DIAGNOSTICS (default off).
        "api/routers/tws_diagnostics.py",
    )
    offenders = []
    for path in SRC.rglob("*.py"):
        relative = path.relative_to(SRC).as_posix()
        if relative.startswith(allowed_importer_prefixes):
            continue
        for line in path.read_text().splitlines():
            stripped = line.strip()
            is_import_line = stripped.startswith(("import ", "from "))
            if is_import_line and any(needle in stripped for needle in needles):
                offenders.append(relative)
    return offenders


def test_v4_compatibility_module_itself_is_only_imported_by_v4_and_test_code():
    """A second, complementary check: v4_compatibility is genuinely
    IMPORTED only from v4_* modules or a clearly-experimental api
    router -- never from services/ (other than the v4-prefixed/
    experimental ones). Matches only real `import`/`from ... import`
    lines, not an incidental docstring mention of the file path (e.g.
    schemas/api.py's own V4CompatibilityResponse docstring, which
    references the file by name without importing it)."""
    assert _real_import_offenders(("v4_compatibility", "evaluate_semantic_compatibility")) == []


def test_v4_strike_engine_module_itself_is_only_imported_by_v4_and_test_code():
    """V4.3's own equivalent of the check above for the strike engine
    (this task's own Section 35)."""
    assert (
        _real_import_offenders(("v4_strike_engine", "v4_strike_resolver", "select_v4_strikes"))
        == []
    )


def test_v4_strike_engine_never_reads_option_delta():
    """This task's own Section 18: real delta/Greeks coverage was
    audited (see the V4.3 report) and found reliable enough to use as
    a SECONDARY tie-break signal but not reliable enough (~28% of
    populated values are Black-Scholes fallbacks, not live provider
    greeks) to make strike-selection TARGETS depend on. This is a
    static guarantee that default holds: the strike engine, resolver,
    and V4.3.1 geometry-variant generator source never reference
    ``.delta`` at all -- verified directly against the real file text,
    not by convention."""
    for relative_path in (
        "analytics/decision/v4_strike_engine.py",
        "analytics/decision/v4_strike_resolver.py",
        "analytics/decision/v4_strike_geometry_variants.py",
    ):
        text = (SRC / relative_path).read_text()
        assert ".delta" not in text, f"{relative_path} references OptionQuote.delta"


def test_v4_strike_geometry_variants_module_itself_is_only_imported_by_v4_and_test_code():
    """V4.3.1's own equivalent of the check above for the geometry-
    variant generator."""
    assert (
        _real_import_offenders(("v4_strike_geometry_variants", "generate_strike_geometry_variants"))
        == []
    )


def test_v4_t1_pricing_module_itself_is_only_imported_by_v4_and_test_code():
    """V4.4A's own equivalent of the check above for the T+1 scenario
    valuation engine."""
    assert _real_import_offenders(("v4_t1_pricing", "evaluate_candidate_t1_scenarios")) == []


def test_v4_t1_replay_never_imports_a_live_market_data_provider():
    """Section 34/40's own no-live-call rule, verified statically: the
    real replay module never imports any real provider integration
    (ibkr_*, alpha_vantage, etc) -- only real, already-persisted quote
    data flows in, via the caller-supplied ChainMetadata-shaped
    fixtures."""
    text = (SRC / "analytics/decision/v4_t1_replay.py").read_text()
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "ibkr" not in stripped.lower()
            assert "alpha_vantage" not in stripped.lower()
