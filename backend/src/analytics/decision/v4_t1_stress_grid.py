"""V4.4C -- tail-stress expansion of the T+1 underlying-move grid.

WHY THIS EXISTS (Section 15). V4.4B's own report named the ±1.0
expected-move envelope as a limitation: real earnings outcomes exceed one
implied move regularly, so a grid that stops at ±1.0 EM does not test tail
behaviour at all. For a benchmark whose whole purpose is executable T+1
economics, and for a ranking hierarchy that puts downside first, never
looking past one expected move leaves the most dangerous region of the
outcome space unmodelled.

THE JUSTIFICATION IS STRUCTURAL, NOT EMPIRICAL. These points exist
because an implied move is roughly a one-sigma expectation and earnings
distributions have fat tails, so outcomes beyond it are ordinary rather
than exotic. They were NOT chosen because DY, NVDA, or DG lost money --
no realized outcome informed the magnitudes, the count, or the
placement, and this module imports no outcome data of any kind
(structurally asserted in tests/test_v4_4c_shadow_isolation.py).

CORE AND STRESS ARE NEVER MIXED (Sections 16, 17). V4.4A's original
seven-point ±1.0 EM grid is preserved byte-for-byte as the CORE grid, and
every statistic V4.4B already ranks on continues to be computed from CORE
scenarios ONLY. Stress points are reported as their own separate
diagnostics. This matters more than it first appears: appending stress
points into the same unweighted pool would silently move the median, the
positive-scenario fraction, and therefore the ranking itself -- a
methodology change disguised as extra data. V4.4B's ranking version stays
frozen precisely because its inputs are untouched.

STRESS POINTS ARE NOT PROBABILITIES. There is no calibrated probability
mass anywhere in this grid, and adding more extreme points does not
create any. A stress result answers "how bad does this get if the move is
much larger than implied", never "how likely is that".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_strike_engine import expected_move_boundary_at_fraction
from analytics.decision.v4_t1_scenario_grid import UnderlyingScenario

#: Bumped from the implicit v1 (V4.4A's core-only grid). Persisted on
#: every shadow candidate so a future reader knows exactly which grid
#: produced a stored statistic -- see Section 16's own versioning rule.
SCENARIO_GRID_VERSION = "v4-t1-scenario-grid-v2-core-plus-stress"

#: Deliberately NOT redefined here -- V4.4A's own core grid is imported
#: and reused so the two can never drift apart.
CORE_GRID_NOTE = (
    "CORE = V4.4A's original 7-point +/-1.0 EM grid, unchanged. Every V4.4B ranking "
    "statistic is computed from CORE scenarios only."
)

#: Stress magnitudes, beyond the core envelope. Round numbers, chosen for
#: structural tail coverage, never fitted.
STRESS_EM_FRACTIONS: tuple[tuple[str, Decimal], ...] = (
    ("STRESS_LARGE_DOWNSIDE_1_5EM", Decimal("-1.5")),
    ("STRESS_EXTREME_DOWNSIDE_2_0EM", Decimal("-2.0")),
    ("STRESS_LARGE_UPSIDE_1_5EM", Decimal("1.5")),
    ("STRESS_EXTREME_UPSIDE_2_0EM", Decimal("2.0")),
)

STRESS_SEMANTICS_NOTE = (
    "DETERMINISTIC STRESS POINTS -- no probability mass, never averaged into core "
    "statistics, never weighted. Justified structurally (earnings moves routinely exceed "
    "one implied move), never by realized outcomes."
)


def build_stress_scenarios(context: ExpectedMoveContext) -> tuple[UnderlyingScenario, ...] | None:
    """Stress-only underlying scenarios. Returns ``None`` when the
    expected-move context genuinely cannot place a boundary -- an honest
    absence, exactly as V4.4A's own core builder behaves, never a
    fabricated fallback move."""
    scenarios: list[UnderlyingScenario] = []
    for label, fraction in STRESS_EM_FRACTIONS:
        side: Literal["up", "down"] = "up" if fraction > 0 else "down"
        boundary = expected_move_boundary_at_fraction(context, side, abs(fraction))
        if boundary is None:
            return None
        scenarios.append(
            UnderlyingScenario(
                label=label,  # type: ignore[arg-type]
                em_fraction=fraction,
                scenario_underlying_price=boundary.price,
                move_dollars=boundary.price - context.spot,
                source=boundary.source,
            )
        )
    return tuple(scenarios)


@dataclass(frozen=True)
class TailStressDiagnostic:
    """Reported alongside -- never inside -- V4.4B's core ranking
    statistics."""

    scenario_grid_version: str
    n_stress_scenarios: int
    n_stress_valued: int
    #: Worst executable T+1 return across stress points only.
    stress_worst_return: Decimal | None
    #: Fraction of stress scenarios that stay non-negative. Named
    #: "survival", not "probability" -- it is a coverage count over
    #: deterministic points, nothing more.
    stress_large_move_survival: Decimal | None
    #: How much worse the stress tail is than the core tail. Positive
    #: means the tail is genuinely worse than the core grid suggested,
    #: which is the entire reason for computing it.
    stress_vs_core_worst_delta: Decimal | None
    note: str
    #: V4 consolidation (Section 26): the raw stress cells themselves, so
    #: the T+1 matrix can be frozen as evidence. Defaulted so every existing
    #: constructor call is unchanged; never read by the ranker.
    results: tuple = ()


def summarize_tail_stress(
    stress_returns: list[Decimal | None],
    core_worst_return: Decimal | None,
) -> TailStressDiagnostic:
    valued = [r for r in stress_returns if r is not None]
    if not valued:
        return TailStressDiagnostic(
            scenario_grid_version=SCENARIO_GRID_VERSION,
            n_stress_scenarios=len(stress_returns),
            n_stress_valued=0,
            stress_worst_return=None,
            stress_large_move_survival=None,
            stress_vs_core_worst_delta=None,
            note="no stress scenario could be valued -- tail behaviour unknown, not assumed fine",
        )
    worst = min(valued)
    survived = sum(1 for r in valued if r >= 0)
    delta = (core_worst_return - worst) if core_worst_return is not None else None
    return TailStressDiagnostic(
        scenario_grid_version=SCENARIO_GRID_VERSION,
        n_stress_scenarios=len(stress_returns),
        n_stress_valued=len(valued),
        stress_worst_return=worst,
        stress_large_move_survival=Decimal(survived) / Decimal(len(valued)),
        stress_vs_core_worst_delta=delta,
        note=STRESS_SEMANTICS_NOTE,
    )
