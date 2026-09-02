"""Versioned decision/settlement timing policy (V4 product consolidation,
2026-09-02).

WHY THIS MODULE EXISTS
----------------------
Until now this project had exactly one wall-clock time for everything:
``earnings_timing.ENTRY_EXIT_TIME = time(15, 55)``, used for the V3
decision/entry observation AND for the T+1 settlement observation, and
mirrored by a single ``_ENTRY_HOUR_ET``/``_ENTRY_MINUTE_ET`` pair that
scheduled all four cron jobs.

The V4 cohort moves its DECISION/ENTRY observation earlier, to ~15:30 ET,
to buy execution runway before the 16:00 close: six configurations have to
be evaluated, and a late scheduler at 15:55 leaves no room to recover.

Two things must NOT move with it:

1. **V3 history and V3's ongoing control cohort.** V3 stays at 15:55 ET.
   Its historical records are evidence and are never relabelled. Moving V3
   now would also break its own cohort continuity mid-flight -- the V3
   series would silently contain two different entry times.
2. **Settlement/exit timing.** The T+1 exit benchmark stays at 15:55 ET for
   BOTH engines. Entry timing and settlement timing are separate policies
   and a change to one must never drag the other along.

A deliberate consequence: V3 and V4 are NOT timestamp-identical cohorts.
V4 observes 25 minutes earlier, so their entry prices are taken from
different moments of the session. That is a real, acknowledged limitation
of any V3-vs-V4 comparison and is recorded here rather than hidden. It was
judged the lesser evil: the alternative -- moving V3 to match -- would
corrupt the control cohort that gives the comparison its meaning.

Every forward record freezes the policy version it ran under, so a future
reader can always tell which clock produced a given observation instead of
inferring it from a timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time

# Stable identities. Chosen once and never renamed -- they are persisted on
# every forward record, so a rename would orphan existing evidence.
V3_TIMING_POLICY_VERSION = "v3-pre-earnings-1555et-v1"
V4_TIMING_POLICY_VERSION = "v4-pre-earnings-1530et-v1"


@dataclass(frozen=True)
class DecisionTimingPolicy:
    """One versioned answer to 'what time of day does this cohort observe?'

    ``entry_time`` is the decision/entry observation; ``exit_time`` is the
    T+1 settlement observation. They are separate fields precisely so that
    changing one cannot silently change the other.
    """

    version: str
    entry_time: time
    exit_time: time
    description: str

    @property
    def entry_hour(self) -> int:
        return self.entry_time.hour

    @property
    def entry_minute(self) -> int:
        return self.entry_time.minute


#: The historical/control policy. V3 has always used this and continues to.
#: Its entry time must never change -- doing so would split the V3 control
#: cohort across two clocks and invalidate its own continuity.
V3_TIMING_POLICY = DecisionTimingPolicy(
    version=V3_TIMING_POLICY_VERSION,
    entry_time=time(15, 55),
    exit_time=time(15, 55),
    description=(
        "V3 official/control cohort: decision and entry observed at 15:55 ET on the "
        "legal pre-earnings trading day; T+1 settlement observed at 15:55 ET on the "
        "first post-earnings trading day."
    ),
)

#: The V4 forward-test policy. Entry moves to 15:30 ET; settlement stays at
#: 15:55 ET deliberately -- see this module's docstring.
V4_TIMING_POLICY = DecisionTimingPolicy(
    version=V4_TIMING_POLICY_VERSION,
    entry_time=time(15, 30),
    exit_time=time(15, 55),
    description=(
        "V4 forward-test cohort: decision and entry observed at 15:30 ET on the legal "
        "pre-earnings trading day, giving ~30 minutes of runway before the close for "
        "six configuration evaluations and for recovery if the scheduler is late. "
        "T+1 settlement remains 15:55 ET, unchanged from V3 -- entry timing and "
        "settlement timing are separate policies."
    ),
)

#: V4-only reset (2026-09-02, effective from the first settlement on
#: 2026-09-03): the T+1 settlement observation moves to 15:30 ET as well, so
#: entry and exit are taken at the same time of day -- never a same-day
#: settlement for an AMC report (D0 15:30 entry -> D+1 15:30 exit; BMO:
#: D-1 15:30 entry -> D0 15:30 exit). The v1 policy above stays in the
#: registry unchanged: rows frozen under it keep their version string, and
#: a settlement taken under v2 records v2 on the settlement row itself --
#: an honest, prospective transition with no rewritten entry evidence.
V4_TIMING_POLICY_V2_VERSION = "v4-1530-entry-1530-t1-settlement-v2"

V4_TIMING_POLICY_V2 = DecisionTimingPolicy(
    version=V4_TIMING_POLICY_V2_VERSION,
    entry_time=time(15, 30),
    exit_time=time(15, 30),
    description=(
        "V4 forward-test cohort, v2: decision and entry observed at 15:30 ET on the legal "
        "pre-earnings trading day; settlement observed at 15:30 ET on the first "
        "post-earnings trading day. Replaces the 15:55 ET settlement of v1 prospectively."
    ),
)

#: The policy every NEW V4 observation runs under. Historical rows resolve
#: their own stored version through get_timing_policy().
V4_ACTIVE_TIMING_POLICY = V4_TIMING_POLICY_V2

_BY_VERSION: dict[str, DecisionTimingPolicy] = {
    V3_TIMING_POLICY.version: V3_TIMING_POLICY,
    V4_TIMING_POLICY.version: V4_TIMING_POLICY,
    V4_TIMING_POLICY_V2.version: V4_TIMING_POLICY_V2,
}


def get_timing_policy(version: str) -> DecisionTimingPolicy:
    """Resolves a persisted policy version back to its definition.

    Raises rather than falling back to a default: a record whose policy
    version is unknown must never be silently reinterpreted under some
    other cohort's clock.
    """
    try:
        return _BY_VERSION[version]
    except KeyError:
        raise ValueError(
            f"Unknown decision timing policy version {version!r}. "
            f"Known versions: {sorted(_BY_VERSION)}"
        ) from None
