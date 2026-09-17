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
    #: Whether a forward decision requires the report's own session (BMO or
    #: AMC) to be known. See V4_TIMING_POLICY_V3 for the defect this closes.
    #: Carried on the policy rather than read from a setting, so every frozen
    #: record says which eligibility rule produced it.
    requires_confirmed_timing: bool = False

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

#: V4-only eligibility change (2026-09-17), prospective from the first
#: window after deployment: an event whose announcement session is not known
#: to be BMO or AMC no longer produces a forward decision at all.
#:
#: The defect it closes. v1/v2 gave an UNKNOWN session the conservative
#: BMO-SHAPED schedule (analytics/earnings_timing.py::
#: compute_entry_exit_schedule, "never assume AMC"): decide D-1 15:30,
#: settle D 15:30. That rule was built to protect the ENTRY, and it does --
#: entering a trading day early is never look-ahead whatever the real
#: session turns out to be. It does not protect the SETTLEMENT. If the
#: company actually reports after D's close, the release happens AFTER the
#: 15:30 settlement observation, so the whole D-1 15:30 -> D 15:30
#: observation spans no earnings at all, and is then graded and published
#: as an earnings result. That is the ORCL contamination shape (an
#: observation over a window containing no report), reached from the timing
#: field instead of from the date field.
#:
#: Guessing a session is therefore not the safe default it looks like, and
#: blocking costs nothing real: measured on this deployment's own calendar
#: (2026-09-17), every >=$10B UPCOMING event with an UNKNOWN session was a
#: far-future fallback-provider placeholder (earliest 2027-05-10), and all
#: 13 genuinely upcoming eligible events carried a corroborated BMO or AMC.
#:
#: This is a blocking rule, never a scheduling one: the entry/exit clock is
#: identical to v2, so an event whose session becomes known simply enters
#: its own legal window on the normal schedule (no early decision, and no
#: decision written after the window has passed).
V4_TIMING_POLICY_V3_VERSION = "v4-1530-entry-1530-t1-settlement-confirmed-timing-v3"

V4_TIMING_POLICY_V3 = DecisionTimingPolicy(
    version=V4_TIMING_POLICY_V3_VERSION,
    entry_time=time(15, 30),
    exit_time=time(15, 30),
    description=(
        "V4 forward-test cohort, v3: identical observation clock to v2 (decision and entry "
        "at 15:30 ET on the legal pre-earnings trading day, settlement at 15:30 ET on the "
        "first post-earnings trading day), with one added eligibility rule -- an event whose "
        "announcement session is not known to be BMO or AMC produces no forward decision, "
        "instead of being scheduled as if it were BMO."
    ),
    requires_confirmed_timing=True,
)

#: The policy every NEW V4 observation runs under. Historical rows resolve
#: their own stored version through get_timing_policy().
V4_ACTIVE_TIMING_POLICY = V4_TIMING_POLICY_V3

_BY_VERSION: dict[str, DecisionTimingPolicy] = {
    V3_TIMING_POLICY.version: V3_TIMING_POLICY,
    V4_TIMING_POLICY.version: V4_TIMING_POLICY,
    V4_TIMING_POLICY_V2.version: V4_TIMING_POLICY_V2,
    V4_TIMING_POLICY_V3.version: V4_TIMING_POLICY_V3,
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
