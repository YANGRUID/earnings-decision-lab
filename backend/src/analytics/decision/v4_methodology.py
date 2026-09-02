"""Options Decision Engine V4.1 -- Methodology Foundation (2026-08-31).

V3 (services/decision_snapshot_freezing.py::ENGINE_VERSION =
"options-decision-engine-v3") is now a frozen control cohort. This module
defines V4's benchmark objective -- the single most important design
correction the forensic audit identified -- and a centralized,
version-tracked methodology object for everything V4 will eventually
implement.

V4 IS EXPERIMENTAL AND CURRENTLY INERT. Nothing in this module, or any
other analytics/decision/v4_*.py module, is imported by
services/decision_engine.py, services/decision_pipeline.py,
services/benchmark_entry_capture.py, or services/scheduler.py. No code
path can produce a real, official V4 DecisionSnapshot, EntryCaptureAttempt,
or EntrySnapshot today -- see core.config.Settings.experimental_engine_v4_enabled
(default False) and core.config.Settings.official_engine_version (default
ENGINE_VERSION, i.e. V3), which a future task will flip only once V4's
remaining methodology stages (see V4Stage below) are actually built.
"""

from dataclasses import dataclass
from datetime import time

from models.enums import ExitPolicy, MarketDataQualityPolicy

ENGINE_VERSION_V4 = "options-decision-engine-v4"


@dataclass(frozen=True)
class BenchmarkObjective:
    """The real thing strategy quality is evaluated against -- explicit
    and documented, in direct response to the forensic audit's Part I
    Section 3 finding: V3's ranking (67 of 100 points) and its entire
    risk-sizing/R-multiple denominator are built on pure expiration-payoff
    intrinsic value, while the real benchmark exits via a real bid/ask
    liquidation one trading session later, nowhere near expiration.

    This is a DEFINITION, not an implementation -- V4.1 does not yet
    build a model that scores or predicts against this objective (that is
    V4.4). It exists so every later V4 stage inherits one, explicit,
    shared answer to "what are we actually trying to get right," instead
    of each stage silently assuming expiration payoff again.
    """

    name: str
    exit_policy: ExitPolicy
    entry_time_et: time
    exit_time_et: time
    pricing_side_long_open: str
    pricing_side_short_open: str
    pricing_side_long_close: str
    pricing_side_short_close: str
    holding_period_description: str
    market_data_quality_policy: MarketDataQualityPolicy
    description: str


T1_POST_EARNINGS_LIQUIDATION_V1 = BenchmarkObjective(
    name="t1_liquidation_v1",
    exit_policy=ExitPolicy.FIRST_POST_EARNINGS_TRADING_DAY_CLOSE,
    entry_time_et=time(15, 55),
    exit_time_et=time(15, 55),
    pricing_side_long_open="ASK",
    pricing_side_short_open="BID",
    pricing_side_long_close="BID",
    pricing_side_short_close="ASK",
    holding_period_description=(
        "Approximately one trading session, never the option's own expiration: entry "
        "~15:55 ET the trading day before the earnings reaction is priced in (see "
        "analytics/earnings_timing.py::compute_entry_exit_schedule), exit ~15:55 ET the "
        "first real trading day after it. Every real settled V3 decision left 1-23 days of "
        "DTE still remaining at that forced exit."
    ),
    market_data_quality_policy=MarketDataQualityPolicy.ALLOW_DELAYED_WITH_LABEL,
    description=(
        "T+1 POST-EARNINGS LIQUIDATION PERFORMANCE. A strategy is evaluated by what it "
        "would actually be worth, at real conservative bid/ask, one trading session after "
        "the event -- NOT by its theoretical expiration payoff. This is the one objective "
        "every future V4 scoring, sizing, and strike-selection stage must be designed "
        "against."
    ),
)


@dataclass(frozen=True)
class V4MethodologyVersion:
    """A single, centralized, auditable record of exactly how much of V4
    exists at any point in its development -- so "V4" never silently
    means something different depending on which module you happen to
    read. Each field is either a real version string or one of the
    explicit placeholders below; nothing here is ever inferred."""

    engine_version: str
    benchmark_objective: str
    capital_semantics: str
    strategy_semantics_version: str
    # V4.2 -- the view<->strategy semantic compatibility engine's own
    # version (analytics/decision/v4_compatibility.py). Distinct from
    # ranking_version below: this module answers "how compatible is this
    # strategy with the stated view," never "what should the final
    # ranking score be" -- that combination is V4.4's job.
    view_strategy_compatibility_version: str
    strike_engine_version: str
    # V4.3.1 -- the geometry-VARIANT generator's own version
    # (analytics/decision/v4_strike_geometry_variants.py). Distinct
    # from strike_engine_version: that field tracks V4.3's single
    # canonical/base geometry per strategy (unchanged, byte for byte,
    # by V4.3.1 -- confirmed via V4.3's own full test suite still
    # passing unmodified); this field tracks the separate, additive
    # bounded-alternative-geometry-set capability layered on top.
    geometry_candidate_version: str
    # V4.4A -- the T+1 scenario valuation engine's own version
    # (analytics/decision/v4_t1_pricing.py). Answers "what does this
    # candidate's economic outcome distribution look like at the real
    # T+1 exit horizon" -- deliberately NOT a score or a rank
    # (ranking_version below remains a separate, still-unimplemented
    # placeholder; V4.4A produces orthogonal measurements only, this
    # task's own Section 38).
    t1_valuation_version: str
    ranking_version: str
    expiration_version: str


NOT_IMPLEMENTED = "not_implemented"
V3_LEGACY = "v3_legacy"

# V4.4A -- t1_valuation_version became real ("t1_pricing_v1",
# analytics/decision/v4_t1_pricing.py).
#
# V4.4B (this task) -- ranking_version is now real
# ("v4-4b-t1-executable-ranking-v1", analytics/decision/v4_4b_ranking.py):
# the first V4 phase permitted to ORDER candidates. Deliberately NOT
# imported from that module: this record is the single auditable
# statement of what V4 is, and it must keep stating a frozen literal even
# if a future edit changes the ranker's own constant -- the resulting
# test failure (tests/test_v4_4b_ranking_isolation.py) is the point,
# since a silently-renamed ranking version would invalidate every replay
# already run against the old one (Section 38).
#
# strike_engine_version/geometry_candidate_version stay exactly as
# V4.3/V4.3.1 left them, and t1_valuation_version stays exactly as V4.4A
# left it -- V4.4B consumes those surfaces, it does not modify them.
# expiration_version remains an explicit placeholder: V4.4B compares
# whatever expirations candidate generation honestly supplies and does
# NOT reintroduce V3's own expiration score (Section 20).
V4_METHODOLOGY = V4MethodologyVersion(
    engine_version=ENGINE_VERSION_V4,
    benchmark_objective=T1_POST_EARNINGS_LIQUIDATION_V1.name,
    capital_semantics="standardized_per_decision_v1",
    strategy_semantics_version="v2",
    view_strategy_compatibility_version="view_strategy_compatibility_v1",
    strike_engine_version="expected_move_v1",
    geometry_candidate_version="geometry_candidate_v1",
    t1_valuation_version="t1_pricing_v1",
    ranking_version="v4-4b-t1-executable-ranking-v1",
    expiration_version=NOT_IMPLEMENTED,
)


@dataclass(frozen=True)
class V4Stage:
    id: str
    title: str
    status: str
    summary: str


# The explicit, ordered deferral list (forensic audit Part II Section 42
# / this task's own Section 25) -- never implemented opportunistically
# ahead of its own stage.
V4_ROADMAP: tuple[V4Stage, ...] = (
    V4Stage(
        "v4.1",
        "Methodology Foundation and Version Isolation",
        "complete",
        "Objective definition, strategy semantics registry, feature contract, capital "
        "terminology, Track Record cohort/terminology fixes, DY source-coherence fix. No "
        "V4 recommendations generated.",
    ),
    V4Stage(
        "v4.2",
        "View <-> Strategy Semantic Compatibility",
        "complete",
        "The deterministic compatibility engine (analytics/decision/v4_compatibility.py) -- "
        "not yet consumed by any ranking weight (that is V4.4). Re-audited and corrected the "
        "V4.1 registry's credit-spread move_intent along the way.",
    ),
    V4Stage(
        "v4.3",
        "Expected-Move-Aware Strike Selection",
        "complete",
        "Deterministic, strategy-specific strike construction (analytics/decision/"
        "v4_strike_engine.py) driven by a single authoritative ExpectedMoveContext -- "
        "replaces V3's pure ATM-index-offset selection for V4 only; V3 itself is untouched. "
        "Delta audited and deliberately not used as a primary signal (see the V4.3 report's "
        "own delta/Greeks audit).",
    ),
    V4Stage(
        "v4.3.1",
        "Target-Aware Chain Coverage & Geometry Candidate Sets",
        "complete",
        "Intermediate fix between V4.3 and V4.4: distinguishes a real listed-chain boundary "
        "(TARGET_NOT_LISTED, confirmed) from a merely-narrow captured window "
        "(TARGET_BEYOND_CAPTURED_WINDOW, ambiguous) -- analytics/decision/"
        "v4_chain_coverage.py -- and adds a bounded set of economically meaningful strike-"
        "geometry VARIANTS per strategy alongside V4.3's own unmodified base geometry -- "
        "analytics/decision/v4_strike_geometry_variants.py. Neither ranks nor scores "
        "variants; that is V4.4's job.",
    ),
    V4Stage(
        "v4.4a",
        "T+1 Post-Earnings Scenario Valuation",
        "complete",
        "Reprices every candidate geometry across a bounded underlying-move x IV-crush "
        "scenario grid at the real T+1 exit horizon (analytics/decision/v4_t1_pricing.py) -- "
        "theoretical model value kept permanently separate from estimated executable exit "
        "value. Orthogonal measurements only: no score, no rank, no winner.",
    ),
    V4Stage(
        "v4.4b",
        "T+1-Objective-Aware Candidate Scoring/Ranking",
        NOT_IMPLEMENTED,
        "Combines V4.2's semantic compatibility, V4.3/V4.3.1's strike geometry, and V4.4A's "
        "own scenario valuation into one final ranking/sizing model scored against "
        "BenchmarkObjective (T+1 liquidation) -- not built here.",
    ),
    V4Stage(
        "v4.5",
        "Shadow Candidate Data Collection",
        NOT_IMPLEMENTED,
        "Implement the real, append-only shadow-candidate pipeline this task's "
        "ShadowCandidateEvaluation interface (analytics/decision/v4_shadow.py) only defines.",
    ),
    V4Stage(
        "later",
        "Data-Driven Ranking / ML",
        NOT_IMPLEMENTED,
        "Only once enough real official + shadow settled observations exist -- optimized "
        "deterministic weights, then logistic regression / LightGBM / XGBoost / "
        "learning-to-rank, in that order.",
    ),
)
