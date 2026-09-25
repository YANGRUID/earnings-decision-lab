"""V4.2 PHASE 2 -- the independent-search methodology identity.

WHY A SECOND METHODOLOGY VERSION RATHER THAN AN EDIT
----------------------------------------------------
Phase 1's challenger answers a narrower question than its name suggests. It
reads the CONTROL's own frozen candidate rows, so its real question is "of the
candidates V4.1 constructed on the single expiry V4.1 chose, is any one worth
taking?". A NO_ACTION under Phase 1 therefore means "V4.1's shortlist was
unattractive", which is not the same claim as "no trade was worth taking".

Phase 2 asks the wider question: it builds its own bounded multi-expiry
candidate universe and lets each of the six configurations choose within it.
That changes the SEARCH SPACE and the SELECTION UNIT. It changes no threshold:
the absolute economic gate, the move-edge rule and the friction model are
imported from Phase 1 unchanged, deliberately, so that any difference in
outcome is attributable to what was searched rather than to what was tolerated.

Because the question changed, the evidence is not comparable row-for-row with
Phase 1's, and the two must never be concatenated into one series. Hence a
separate methodology version stamped on every row, and a separate activation
boundary below.

WHAT THIS VERSIONS
------------------
Everything a reader would need to reproduce a Phase-2 decision: the candidate
universe policy, the expiry ladder, the ranking policy, the per-configuration
risk policy, the move-edge and viability gates, the friction model and the
timing policy. Each is the real version constant owned by its own module --
never a second copy that could drift.
"""

from __future__ import annotations

from dataclasses import dataclass

from analytics.decision.v4_2_earnings_friction import EARNINGS_FRICTION_VERSION
from analytics.decision.v4_2_expiry_ladder import EXPIRY_LADDER_VERSION
from analytics.decision.v4_2_viability import MOVE_EDGE_VERSION, VIABILITY_GATE_VERSION
from analytics.decision.v4_configurations import V4_CONFIGURATION_VERSION
from analytics.decision.v4_strategy_semantics import STRATEGY_SEMANTICS_VERSION

#: The Phase-2 methodology identity. Stamped on every Phase-2 decision row.
#: Phase-1 rows keep their own identity (see PHASE_1_METHODOLOGY) and are
#: never relabelled.
PHASE_2_METHODOLOGY = "v4.2-independent-search-v1"

#: What Phase-1 evidence is, now that a second phase exists. Phase-1 rows
#: written before this constant existed carry NULL, which reads as Phase 1 --
#: see the backfill note on the migration. Nothing about those rows changes.
PHASE_1_METHODOLOGY = "v4.2-shared-candidate-v1"

#: Phase 1, after the per-configuration risk cap was made to bind (2026-09-24).
#:
#: Not cosmetic. Under v1 the cap could never fire -- the per-configuration
#: check was never passed each candidate's max loss -- so every one of the 15
#: v1 decisions was taken by a policy where a configuration could select, and
#: size, a structure risking several times its own limit; four of the twelve
#: entries were frozen that way. Rows written after the fix come from a policy
#: that refuses those structures, and on the two events that actioned it
#: changes both what was chosen and who chose anything at all.
#:
#: Two policies in one column with nothing marking the boundary is precisely
#: the reading error the methodology version exists to prevent, so the boundary
#: is written into the data rather than left to a changelog. The 15 existing
#: rows are NOT restamped: they were taken under v1 and must keep saying so.
PHASE_1_METHODOLOGY_V2 = "v4.2-shared-candidate-v2"

#: Every identity that means "Phase 1, the shared-candidate challenger".
#: NULL means Phase 1 too, and is handled separately at each call site because
#: SQL cannot match it with IN.
PHASE_1_METHODOLOGIES: tuple[str, ...] = (PHASE_1_METHODOLOGY, PHASE_1_METHODOLOGY_V2)

#: The candidate-universe policy Phase 2 applies. Bumped if the universe's
#: construction rule changes -- not for a refactor.
CANDIDATE_UNIVERSE_VERSION = "v4_2_independent_universe_v1"

#: How the six configurations rank within the shared universe.
CONFIG_RANKING_VERSION = "v4_2_per_configuration_ranking_v1"


@dataclass(frozen=True)
class Phase2MethodologyVersions:
    """Every sub-version a Phase-2 row is reproducible from."""

    methodology_version: str = PHASE_2_METHODOLOGY
    candidate_universe_version: str = CANDIDATE_UNIVERSE_VERSION
    expiry_ladder_version: str = EXPIRY_LADDER_VERSION
    ranking_version: str = CONFIG_RANKING_VERSION
    configuration_version: str = V4_CONFIGURATION_VERSION
    move_edge_version: str = MOVE_EDGE_VERSION
    viability_gate_version: str = VIABILITY_GATE_VERSION
    friction_version: str = EARNINGS_FRICTION_VERSION
    strategy_registry_version: str = STRATEGY_SEMANTICS_VERSION

    @property
    def timing_policy_version(self) -> str:
        """The forward window's own clock. Imported at call time so this
        module never pins a stale copy of the active policy."""
        from analytics.decision_timing_policy import V4_ACTIVE_TIMING_POLICY  # noqa: PLC0415

        return V4_ACTIVE_TIMING_POLICY.version

    def as_dict(self) -> dict[str, str]:
        return {
            "methodology_version": self.methodology_version,
            "candidate_universe_version": self.candidate_universe_version,
            "expiry_ladder_version": self.expiry_ladder_version,
            "ranking_version": self.ranking_version,
            "configuration_version": self.configuration_version,
            "move_edge_version": self.move_edge_version,
            "viability_gate_version": self.viability_gate_version,
            "friction_version": self.friction_version,
            "strategy_registry_version": self.strategy_registry_version,
            "timing_policy_version": self.timing_policy_version,
        }


PHASE_2_VERSIONS = Phase2MethodologyVersions()


__all__ = [
    "CANDIDATE_UNIVERSE_VERSION",
    "CONFIG_RANKING_VERSION",
    "PHASE_1_METHODOLOGIES",
    "PHASE_1_METHODOLOGY",
    "PHASE_1_METHODOLOGY_V2",
    "PHASE_2_METHODOLOGY",
    "PHASE_2_VERSIONS",
    "Phase2MethodologyVersions",
]
