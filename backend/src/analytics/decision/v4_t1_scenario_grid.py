"""Options Decision Engine V4.4A -- T+1 Scenario Grid (2026-09-03).

Builds the two scenario dimensions this task's own Section 6 requires:
a POST-EARNINGS UNDERLYING PRICE grid (Section 7) and a POST-EARNINGS
IMPLIED VOLATILITY grid (Sections 9-10). Neither is a single
deterministic point -- both are bounded, deterministic, documented
grids, never fit to the seven realized V3 losses (Section 35).

UNDERLYING-MOVE GRID (Section 7): reuses the SAME normalized expected-
move-unit convention already established and tested in V4.3/V4.3.1's
own strike-geometry variants (0.25/0.5/1.0 EM) -- not new arbitrary
numbers invented for this task. Built via
``analytics.decision.v4_strike_engine.expected_move_boundary_at_fraction``,
the exact same implied-move-preferred/historical-median-fallback
cascade V4.3's base geometry already uses, so a scenario grid and a
strike target can never silently disagree about what "the expected
move" means.

PROBABILITY WEIGHTS ARE DELIBERATELY NOT ASSIGNED HERE (Section 8):
this codebase has no real volatility-skew capability (confirmed by
direct audit, see the V4.4A report's own Section A) and per-ticker
historical move samples are real but thin (9-46 observations, per
V4.3's own replay data) -- nowhere near enough to responsibly convert
into per-scenario numeric probabilities without inventing false
precision. Every scenario in the grids below is therefore UNWEIGHTED
by construction; ``analytics/decision/v4_t1_pricing.py``'s distribution
summary uses "scenario-average return," never "expected return," for
exactly this reason (Section 19's own mandatory terminology rule).

IV-CRUSH GRID (Sections 9-10): real, paired pre-event/post-event IV
data exists in this project's own history for exactly 6 real earnings
decisions (see the V4.4A report's own Section A for the query and
real values) -- real, but far too thin to calibrate a distribution.
The three named scenario shocks below (``STRONG_CRUSH``,
``NORMAL_CRUSH``, ``WEAK_CRUSH_OR_ELEVATED``) are round, clean,
HEURISTIC/UNCALIBRATED multipliers chosen only to span the REAL
observed directional range of those 6 pairs (roughly -47% to +15%) --
never precision-fit to match any one of them, and never chosen with
knowledge of which of those decisions later realized a profit or loss
(Section 35's own anti-lookahead rule: this is IV market data, not
settlement/outcome data, but it is still built from pre-event-style
reasoning about volatility behavior, never "would this shock have made
CRM/VEEV/DG look better").

EACH LEG KEEPS ITS OWN REAL ENTRY IV (Section 11): no real skew-curve
capability exists in this codebase (confirmed absent by direct audit),
so this module does NOT model a reshaped skew curve. Instead, the
crush multiplier is applied to each leg's OWN real, already-observed
entry IV (never one blanket ATM IV forced onto every leg) -- a
transparent, limited form of skew preservation (each leg's real
relative IV difference at entry survives into the scenario), disclosed
honestly as a simplification, never presented as a true skew model.
"""

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from analytics.decision.v4_expected_move import ExpectedMoveContext
from analytics.decision.v4_strike_engine import expected_move_boundary_at_fraction

T1_SCENARIO_GRID_VERSION = "t1_scenario_grid_v1"

UnderlyingMoveLabel = Literal[
    "LARGE_DOWNSIDE",
    "MODERATE_DOWNSIDE",
    "SMALL_DOWNSIDE",
    "FLAT",
    "SMALL_UPSIDE",
    "MODERATE_UPSIDE",
    "LARGE_UPSIDE",
]

IVScenarioLabel = Literal["STRONG_CRUSH", "NORMAL_CRUSH", "WEAK_CRUSH_OR_ELEVATED"]

FLAT_NO_MOVE = "FLAT_NO_MOVE"

_UNDERLYING_MOVE_GRID: tuple[tuple[UnderlyingMoveLabel, Decimal], ...] = (
    ("LARGE_DOWNSIDE", Decimal("-1")),
    ("MODERATE_DOWNSIDE", Decimal("-0.5")),
    ("SMALL_DOWNSIDE", Decimal("-0.25")),
    ("FLAT", Decimal("0")),
    ("SMALL_UPSIDE", Decimal("0.25")),
    ("MODERATE_UPSIDE", Decimal("0.5")),
    ("LARGE_UPSIDE", Decimal("1")),
)

# HEURISTIC / UNCALIBRATED (Section 10's own required label). Chosen to
# span the real directional range found in this project's own n=6
# pre/post-event IV pairs (see this module's docstring) -- clean, round
# multipliers, never precision-fit to any one of those 6 real values.
IV_CRUSH_SCENARIO_GRID: tuple[tuple[IVScenarioLabel, Decimal], ...] = (
    ("STRONG_CRUSH", Decimal("0.55")),
    ("NORMAL_CRUSH", Decimal("0.75")),
    ("WEAK_CRUSH_OR_ELEVATED", Decimal("1.10")),
)


@dataclass(frozen=True)
class UnderlyingScenario:
    label: UnderlyingMoveLabel
    em_fraction: Decimal
    scenario_underlying_price: Decimal
    move_dollars: Decimal
    source: str


def build_underlying_scenarios(
    context: ExpectedMoveContext,
) -> tuple[UnderlyingScenario, ...] | None:
    """Returns None only when neither implied move nor historical
    median move exists -- an honest absence (Section 4: "do not
    fabricate absent values"), never a fabricated grid."""
    scenarios: list[UnderlyingScenario] = []
    for label, fraction in _UNDERLYING_MOVE_GRID:
        if fraction == 0:
            scenarios.append(
                UnderlyingScenario(label, fraction, context.spot, Decimal(0), FLAT_NO_MOVE)
            )
            continue
        side: Literal["up", "down"] = "up" if fraction > 0 else "down"
        boundary = expected_move_boundary_at_fraction(context, side, abs(fraction))
        if boundary is None:
            return None
        scenarios.append(
            UnderlyingScenario(
                label=label,
                em_fraction=fraction,
                scenario_underlying_price=boundary.price,
                move_dollars=boundary.price - context.spot,
                source=boundary.source,
            )
        )
    return tuple(scenarios)


@dataclass(frozen=True)
class IVScenario:
    label: IVScenarioLabel
    multiplier: Decimal
    source: str = "HEURISTIC_UNCALIBRATED"


def build_iv_scenarios() -> tuple[IVScenario, ...]:
    """Deterministic, evidence-informed-but-not-fitted -- see this
    module's own docstring. Never None: unlike the underlying-move
    grid, this grid needs no per-candidate evidence to construct (it is
    applied multiplicatively to whatever entry IV each leg already
    has), so it is always available."""
    return tuple(IVScenario(label=label, multiplier=mult) for label, mult in IV_CRUSH_SCENARIO_GRID)


def scenario_leg_iv(entry_iv: Decimal | None, iv_scenario: IVScenario) -> Decimal | None:
    """Section 11 -- applies the crush multiplier to THIS leg's OWN
    real entry IV, never a blanket ATM IV. None when the leg has no
    real entry IV to scale (never fabricated)."""
    if entry_iv is None:
        return None
    return entry_iv * iv_scenario.multiplier


@dataclass(frozen=True)
class IVCrushDiagnostic:
    """Section 9's own real, descriptive (never calibrated) diagnostic
    -- built once from this project's real n=6 paired pre/post-event
    IV observations. Purely informational; never consumed by
    ``build_iv_scenarios`` or any repricing function."""

    sample_n: int
    min_crush_ratio: Decimal
    median_crush_ratio: Decimal
    max_crush_ratio: Decimal
    note: str


def summarize_iv_crush_diagnostic(
    pre_post_iv_pairs: list[tuple[Decimal, Decimal]],
) -> IVCrushDiagnostic | None:
    """``pre_post_iv_pairs`` should be real (pre_event_atm_iv,
    post_event_iv) values from real, already-persisted
    DecisionSnapshot/ExitSnapshot rows -- never fabricated, never a
    live re-fetch. Returns None when the list is empty. crush_ratio =
    post/pre - 1 (negative = crush, positive = IV increased -- both
    real, observed outcomes; see this module's own docstring for why a
    real increase is not treated as an anomaly to discard)."""
    if not pre_post_iv_pairs:
        return None
    ratios = sorted((post / pre - 1) for pre, post in pre_post_iv_pairs if pre > 0)
    if not ratios:
        return None
    n = len(ratios)
    median_ratio = ratios[n // 2] if n % 2 == 1 else (ratios[n // 2 - 1] + ratios[n // 2]) / 2
    return IVCrushDiagnostic(
        sample_n=n,
        min_crush_ratio=ratios[0],
        median_crush_ratio=median_ratio,
        max_crush_ratio=ratios[-1],
        note=(
            f"n={n} real paired pre/post-event IV observations -- descriptive only, far too "
            "thin to calibrate a distribution. Never consumed by build_iv_scenarios; the "
            "HEURISTIC_UNCALIBRATED grid there is informed by, not fit to, this range."
        ),
    )
