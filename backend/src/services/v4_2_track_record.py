"""V4.2 CHALLENGER forward track record -- a SEPARATE cohort, never merged.

The single most important property of this module is what it does not do: no
query here can return a V4.1 row, and nothing V4.1 reads can return a
challenger row. They live in different tables and are counted by different
code. A challenger outcome appearing inside a control statistic would not be
a display bug, it would destroy the comparison the challenger exists for.

The unit of analysis is the EVENT. Six configurations of one earnings event
are six sizings of one forecast, not six independent observations, and
reporting "42 outcomes" from seven events is how a tiny sample is made to look
like evidence. Event-level counts come first here; configuration-level
statistics are reported separately and are explicitly labelled as such.

Every aggregate carries its own N and a tiny-sample warning derived from that
N rather than from a hardcoded string. Nothing in this module ranks V4.1
against V4.2 or calls either one better: with the sample sizes involved that
claim cannot be supported, and the vocabulary stays CONTROL and CHALLENGER.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Session

from models.v4_2_challenger import (
    V42ChallengerConfigEntry,
    V42ChallengerConfigResult,
    V42ChallengerConfigSettlement,
    V42ChallengerDecision,
)
from services.v4_settlement_quality import (
    GRADE_SEVERITY,
    settlement_grade,
    summarize_settlement_quality,
)

#: Below this many EVENTS, every derived rate is a description of what
#: happened, not an estimate of what will happen.
TINY_EVENT_N = 20
#: Below this many settled configurations the same is true of outcome stats.
TINY_OUTCOME_N = 30


@dataclass
class ActionBreakdown:
    events_observed: int = 0
    action_events: int = 0
    no_action_events: int = 0
    failed_events: int = 0
    no_action_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def action_rate(self) -> float | None:
        """None, not 0.0, when nothing was observed: an unmeasured rate and a
        measured zero are different facts."""
        if not self.events_observed:
            return None
        return self.action_events / self.events_observed


@dataclass
class OutcomeStats:
    settled: int = 0
    wins: int = 0
    losses: int = 0
    flat: int = 0
    median_standardized_return: Decimal | None = None
    median_capital_used_return: Decimal | None = None
    total_realized_pnl: Decimal | None = None

    @property
    def win_rate(self) -> float | None:
        return None if not self.settled else self.wins / self.settled


@dataclass
class ChallengerTrackRecord:
    methodology: str = "CHALLENGER"
    cohort: str = "v4_2_parallel_shadow"
    actions: ActionBreakdown = field(default_factory=ActionBreakdown)
    entries_observed: int = 0
    entries_failed: int = 0
    settlements_due: int = 0
    settled: int = 0
    settlement_failed: int = 0
    outcomes: OutcomeStats = field(default_factory=OutcomeStats)
    executable_outcomes: OutcomeStats = field(default_factory=OutcomeStats)
    settlement_quality: dict[str, int] = field(default_factory=dict)
    by_configuration: dict[str, dict] = field(default_factory=dict)
    by_strategy: dict[str, dict] = field(default_factory=dict)
    by_expiry_ladder_position: dict[str, dict] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return Decimal(str(statistics.median([float(v) for v in values])))


def _outcome_stats(rows: list[V42ChallengerConfigSettlement]) -> OutcomeStats:
    stats = OutcomeStats(settled=len(rows))
    standardized: list[Decimal] = []
    used: list[Decimal] = []
    total = Decimal(0)
    for row in rows:
        pnl = row.realized_pnl or Decimal(0)
        total += pnl
        if pnl > 0:
            stats.wins += 1
        elif pnl < 0:
            stats.losses += 1
        else:
            stats.flat += 1
        if row.return_on_standardized_capital is not None:
            standardized.append(row.return_on_standardized_capital)
        if row.return_on_capital_used is not None:
            used.append(row.return_on_capital_used)
    stats.median_standardized_return = _median(standardized)
    stats.median_capital_used_return = _median(used)
    stats.total_realized_pnl = total if rows else None
    return stats


def build_challenger_track_record(db: Session) -> ChallengerTrackRecord:
    """The challenger's own forward record. Reads challenger tables only."""
    record = ChallengerTrackRecord()

    decisions = db.query(V42ChallengerDecision).order_by(V42ChallengerDecision.id).all()
    record.actions.events_observed = len(decisions)
    for decision in decisions:
        if decision.status == "RANKED":
            record.actions.action_events += 1
        elif decision.status == "FAILED":
            record.actions.failed_events += 1
        else:
            record.actions.no_action_events += 1
            reason = (decision.no_action_reason or "unspecified").strip()
            # Group by the reason's leading clause: the detail after it names
            # specific candidates and would give every event its own bucket.
            key = reason.split(";")[0].split("(")[0].strip()[:80] or "unspecified"
            record.actions.no_action_reasons[key] = record.actions.no_action_reasons.get(key, 0) + 1

    entries = db.query(V42ChallengerConfigEntry).all()
    record.entries_observed = len([e for e in entries if e.status == "OBSERVED"])
    record.entries_failed = len([e for e in entries if e.status != "OBSERVED"])

    settlements = db.query(V42ChallengerConfigSettlement).all()
    # Settlement OF RECORD per configuration: a failed attempt that a later
    # recovery superseded must not be counted twice.
    of_record: dict[int, V42ChallengerConfigSettlement] = {}
    for row in settlements:
        result_id = row.challenger_config_result_id
        current = of_record.get(result_id)
        if current is None or (row.status == "SETTLED" and current.status != "SETTLED"):
            of_record[result_id] = row
        elif current.status != "SETTLED" and row.id > current.id:
            of_record[result_id] = row
    rows = list(of_record.values())
    settled_rows = [r for r in rows if r.status == "SETTLED"]
    record.settled = len(settled_rows)
    record.settlement_failed = len(rows) - len(settled_rows)
    record.settlements_due = record.entries_observed - len(rows)

    record.outcomes = _outcome_stats(settled_rows)
    executable = [r for r in settled_rows if settlement_grade(r) == "EXECUTABLE_BID_ASK"]
    record.executable_outcomes = _outcome_stats(executable)
    quality = summarize_settlement_quality(rows)
    record.settlement_quality = {grade: quality.counts.get(grade, 0) for grade in GRADE_SEVERITY}

    # ---- configuration-level, reported SEPARATELY from the event counts ----
    configs = db.query(V42ChallengerConfigResult).all()
    settled_by_config: dict[str, list[V42ChallengerConfigSettlement]] = {}
    for row in settled_rows:
        settled_by_config.setdefault(row.configuration_key, []).append(row)
    for config in configs:
        bucket = record.by_configuration.setdefault(
            config.configuration_key,
            {"evaluated": 0, "action": 0, "no_action": 0, "settled": 0},
        )
        bucket["evaluated"] += 1
        if config.status == "RANKED":
            bucket["action"] += 1
        else:
            bucket["no_action"] += 1
    for config_key, group in settled_by_config.items():
        bucket = record.by_configuration.setdefault(
            config_key, {"evaluated": 0, "action": 0, "no_action": 0, "settled": 0}
        )
        bucket["settled"] = len(group)
        stats = _outcome_stats(group)
        bucket["wins"] = stats.wins
        bucket["losses"] = stats.losses
        bucket["median_standardized_return"] = (
            None
            if stats.median_standardized_return is None
            else str(stats.median_standardized_return)
        )

    entry_by_result = {e.challenger_config_result_id: e for e in entries}
    for row in settled_rows:
        entry = entry_by_result.get(row.challenger_config_result_id)
        strategy = row.candidate_id.split(":")[0] if row.candidate_id else "unknown"
        bucket = record.by_strategy.setdefault(strategy, {"settled": 0, "wins": 0, "losses": 0})
        bucket["settled"] += 1
        pnl = row.realized_pnl or Decimal(0)
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
        rung = (
            "unknown"
            if entry is None or entry.expiry_ladder_position is None
            else str(entry.expiry_ladder_position)
        )
        ladder = record.by_expiry_ladder_position.setdefault(
            rung, {"settled": 0, "wins": 0, "losses": 0, "median_dte_at_settlement": None}
        )
        ladder["settled"] += 1
        if pnl > 0:
            ladder["wins"] += 1
        elif pnl < 0:
            ladder["losses"] += 1

    # ---- mandatory tiny-N warnings, derived from the real counts ----------
    n_events = record.actions.events_observed
    if n_events < TINY_EVENT_N:
        record.warnings.append(
            f"{n_events} natural event(s) observed. Every rate below describes what "
            "happened, and none of them supports an inference about what will happen."
        )
    if record.outcomes.settled and record.outcomes.settled < TINY_OUTCOME_N:
        record.warnings.append(
            f"{record.outcomes.settled} settled configuration outcome(s) across "
            f"{n_events} event(s). Configuration outcomes are NOT independent "
            "observations: they are the same forecast at different sizes."
        )
    if not record.outcomes.settled and record.entries_observed:
        record.warnings.append(
            "Positions are frozen but nothing has settled yet: no realized "
            "challenger outcome exists to compare against the control."
        )
    if record.settlement_quality.get("MARKET_CLOSE_FALLBACK", 0) or record.settlement_quality.get(
        "EXPIRATION_INTRINSIC_AT_CLOSE", 0
    ):
        record.warnings.append(
            "Some outcomes were priced at an end-of-day closing mark or expiration "
            "intrinsic value rather than an executable bid/ask. Those are not fills; "
            "the executable-only view excludes them."
        )
    return record


__all__ = [
    "TINY_EVENT_N",
    "TINY_OUTCOME_N",
    "ActionBreakdown",
    "ChallengerTrackRecord",
    "OutcomeStats",
    "build_challenger_track_record",
]
